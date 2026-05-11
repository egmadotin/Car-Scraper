import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

# MongoDB Atlas Connection (Remote)
MONGODB_URI = os.getenv("MONGODB_URI")
# MONGODB_URI = "mongodb://localhost:27017" # Local fallback commented out

DATABASE_NAME = os.getenv("DATABASE_NAME", "prodemand_db")

if not MONGODB_URI:
    raise ValueError("MONGODB_URI not found in environment variables!")

client = AsyncIOMotorClient(MONGODB_URI)
db = client[DATABASE_NAME]

CATEGORY_MAP = {
    "tsbs": "tsbs",
    "specs": "specs",
    "adas": "adas",
    "fluids": "fluids",
    "tires": "tires",
    "resets": "resets",
    "dtcs": "dtcs",
    "wiring": "wiring",
    "locations": "locations",
    "tests": "tests",
    "manual": "manuals"
}
