from builtins import Exception, bool, classmethod, int, str
from datetime import datetime, timezone
from operator import or_
import secrets
from typing import Optional, Dict, List
from sqlalchemy import or_
from pydantic import ValidationError
from sqlalchemy import func, null, update, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_email_service, get_settings
from app.models.user_model import User
from app.schemas.user_schemas import UserCreate, UserUpdate
from app.utils.nickname_gen import generate_nickname
from app.utils.security import generate_verification_token, hash_password, verify_password
from uuid import UUID
from app.services.email_service import EmailService
from app.models.user_model import UserRole
import logging
from pydantic import BaseModel, EmailStr
from typing import Optional
from sqlalchemy import cast, String


settings = get_settings()
logger = logging.getLogger(__name__)
class UserLogin(BaseModel):
    email: EmailStr
    password: str
class UserService:
    @classmethod
    async def _execute_query(cls, session: AsyncSession, query):
        try:
            result = await session.execute(query)
            await session.commit()
            return result
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")
            await session.rollback()
            return None

    @classmethod
    async def _fetch_user(cls, session: AsyncSession, **filters) -> Optional[User]:
        query = select(User).filter_by(**filters)
        result = await cls._execute_query(session, query)
        return result.scalars().first() if result else None

    @classmethod
    async def get_by_id(cls, session: AsyncSession, user_id: UUID) -> Optional[User]:
        return await cls._fetch_user(session, id=user_id)

    @classmethod
    async def get_by_nickname(cls, session: AsyncSession, nickname: str) -> Optional[User]:
        return await cls._fetch_user(session, nickname=nickname)

    @classmethod
    async def get_by_email(cls, session: AsyncSession, email: str) -> Optional[User]:
        return await cls._fetch_user(session, email=email)

    @classmethod
    async def create(cls, session: AsyncSession, user_data: Dict[str, str], email_service: EmailService) -> Optional[User]:
        try:
            validated_data = UserCreate(**user_data).model_dump()
            existing_user = await cls.get_by_email(session, validated_data['email'])
            if existing_user:
                logger.error("User with given email already exists.")
                return None
            validated_data['hashed_password'] = hash_password(validated_data.pop('password'))
            new_user = User(**validated_data)
            new_user.nickname = generate_nickname()
            user_count = await cls.count(session)
            new_user.role = UserRole.ADMIN if user_count == 0 else UserRole.ANONYMOUS
            logger.info(f"User Role: {new_user.role}")
            if new_user.role == UserRole.ADMIN:
                new_user.email_verified = True
            else:
                new_user.verification_token = generate_verification_token()
            session.add(new_user)
            await session.commit()
            await email_service.send_verification_email(new_user)
            return new_user
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")
            await session.rollback()
            return None
        except Exception as e:
            logger.error(f"Error during user creation: {e}")
            return None

    @classmethod
    async def update(cls, session: AsyncSession, user_id: UUID, update_data: Dict[str, str]) -> Optional[User]:
        try:
            validated_data = UserUpdate(**update_data).model_dump(exclude_unset=True)

            if 'password' in update_data:  # Only hash the password if it's being updated
                validated_data['hashed_password'] = hash_password(validated_data.pop('password'))

            query = update(User).where(User.id == user_id).values(**validated_data).execution_options(synchronize_session="fetch")
            await cls._execute_query(session, query)
            updated_user = await cls.get_by_id(session, user_id)
            if updated_user:
                logger.info(f"User {user_id} updated successfully.")
                return updated_user
            else:
                logger.error(f"User {user_id} not found after update attempt.")
            return None
        except Exception as e:  # Broad exception handling for debugging
            logger.error(f"Error during user update: {e}")
            return None

    @classmethod
    async def delete(cls, session: AsyncSession, user_id: UUID) -> bool:
        user = await cls.get_by_id(session, user_id)
        if not user:
            logger.info(f"User with ID {user_id} not found.")
            return False
        await session.delete(user)
        await session.commit()
        return True


    @classmethod
    async def list_users(
        cls,
        session: AsyncSession,
        skip: int = 0,
        limit: int = 10,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        registration_date: Optional[datetime] = None
    ) -> List[User]:
        query = select(User).offset(skip).limit(limit)

        if first_name:
            query = query.where(User.first_name.contains(first_name))

        if last_name:
            query = query.where(User.last_name.contains(last_name))

        if email:
            query = query.where(User.email.contains(email))

        if role:
            query = query.where(cast(User.role, String).contains(role))

        if status:
            query = query.where(User.status == status)

        if registration_date:
            query = query.where(User.registration_date >= registration_date)

        result = await cls._execute_query(session, query)
        return result.scalars().all() if result else []

    @classmethod
    async def register_user(cls, session: AsyncSession, user_data: Dict[str, str], get_email_service) -> Optional[User]:
        return await cls.create(session, user_data, get_email_service)


    @classmethod
    async def login_user(cls, session: AsyncSession, email: str, password: str) -> Optional[User]:
        # Validate the input data
        try:
            UserLogin(email=email, password=password)
        except ValueError as e:
            logger.error(f"Invalid input data: {e}")
            return None

        user = await cls.get_by_email(session, email)
        if user:
            if user.email_verified is False:
                return None
            if user.is_locked:
                return None
            if verify_password(password, user.hashed_password):
                user.failed_login_attempts = 0
                user.last_login_at = datetime.now(timezone.utc)
                session.add(user)
                await session.commit()
                return user
            else:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= settings.max_login_attempts:
                    user.is_locked = True
                session.add(user)
                await session.commit()
        return None

    @classmethod
    async def is_account_locked(cls, session: AsyncSession, email: str) -> bool:
        user = await cls.get_by_email(session, email)
        return user.is_locked if user else False


    @classmethod
    async def reset_password(cls, session: AsyncSession, user_id: UUID, new_password: str) -> bool:
        hashed_password = hash_password(new_password)
        user = await cls.get_by_id(session, user_id)
        if user:
            user.hashed_password = hashed_password
            user.failed_login_attempts = 0  # Resetting failed login attempts
            user.is_locked = False  # Unlocking the user account, if locked
            session.add(user)
            await session.commit()
            return True
        return False

    @classmethod
    async def verify_email_with_token(cls, session: AsyncSession, user_id: UUID, token: str) -> bool:
        user = await cls.get_by_id(session, user_id)
        if user is None:
            logging.error(f"No user found with id {user_id}")
            return False
        if user.verification_token != token:
            logging.error(f"Token mismatch for user {user_id}. Expected: {user.verification_token}, Received: {token}")
            return False
        user.email_verified = True
        user.verification_token = None  # Clear the token once used
        user.role = UserRole.AUTHENTICATED
        session.add(user)
        await session.commit()
        return True

    @classmethod
    async def count(
        cls,
        session: AsyncSession,
        search: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        registration_date: Optional[datetime] = None,
    ) -> int:
        query = select(func.count()).select_from(User)

        if search:
            try:
                search_int = int(search)
                query = query.where(
                    or_(
                        User.id == search_int,
                        User.email.contains(search),
                        cast(User.role, String).contains(search),
                        User.first_name.contains(search),
                        User.last_name.contains(search),
                    )
                )
            except ValueError:
                query = query.where(
                    or_(
                        User.email.contains(search),
                        cast(User.role, String).contains(search),
                        User.first_name.contains(search),
                        User.last_name.contains(search),
                    )
                )

        if first_name:
            query = query.where(User.first_name == first_name)

        if last_name:
            query = query.where(User.last_name == last_name)

        if email:
            query = query.where(User.email == email)

        if role:
            query = query.where(User.role == role)

        if status:
            query = query.where(User.status == status)

        if registration_date:
            query = query.where(User.registration_date >= registration_date)

        result = await session.execute(query)
        count = result.scalar()
        return count

    @classmethod
    async def unlock_user_account(cls, session: AsyncSession, user_id: UUID) -> bool:
        user = await cls.get_by_id(session, user_id)
        if user and user.is_locked:
            user.is_locked = False
            user.failed_login_attempts = 0  # Optionally reset failed login attempts
            session.add(user)
            await session.commit()
            return True
        return False
