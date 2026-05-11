import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "prodemand_db")

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
