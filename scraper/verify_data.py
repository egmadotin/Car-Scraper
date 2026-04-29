from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DATABASE_NAME", "prodemand_selector")

client = MongoClient(MONGO_URI)
# Check both potential databases
for db_name in ["prodemand_db", "prodemand_selector"]:
    db = client[db_name]
    print(f"\nChecking database: {db_name}")
    
    vehicles_with_summary = db.vehicles.count_documents({"dashboard_summary": {"$exists": True}})
    print(f"Vehicles with dashboard summary: {vehicles_with_summary}")

    collections = ["tsbs", "specs", "fluids", "manuals", "wiring", "locations", "tests", "adas", "tires_lifting", "resets", "dtcs"]
    print(f"Tab Data Counts:")
    for col in collections:
        count = db[col].count_documents({})
        print(f"  {col.capitalize()}: {count}")
