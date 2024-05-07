from builtins import str
import random
import uuid

def generate_nickname() -> str:
    """Generate a URL-safe nickname using adjectives and animal names."""
    adjectives = ["clever", "jolly", "brave", "sly", "gentle"]
    animals = ["panda", "fox", "raccoon", "koala", "lion"]
    unique_id = uuid.uuid4().hex[:6]  # Generate a unique ID
    return f"{random.choice(adjectives)}_{random.choice(animals)}_{unique_id}"