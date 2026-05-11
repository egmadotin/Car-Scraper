from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
import os
import subprocess
import sys
import time
from typing import Optional
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Vehicle Hierarchy Explorer")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# MongoDB Connection
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client[os.getenv("DATABASE_NAME", "prodemand_db")]

# ─── CATEGORY MAPPING ────────────────────────────────────────────────────────
CATEGORY_MAP = {
    "tsbs": "tsbs",
    "specs": "specs",
    "adas": "adas",
    "fluids": "fluids",
    "tires": "tires_lifting",
    "resets": "resets",
    "dtcs": "dtcs",
    "wiring": "wiring",
    "locations": "locations",
    "tests": "tests",
    "manual": "manuals",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  VEHICLE CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/")
async def index(request: Request):
    # Fetch years from vehicles
    vehicle_years = await db.vehicles.distinct("year")
    
    # Fetch years from the dedicated 'years' collection
    years_doc = await db.years.find_one({"type": "year_list"}) # Assuming type is year_list or similar
    if not years_doc:
        years_doc = await db.years.find_one() # Fallback to first doc
    
    all_years = set(vehicle_years)
    if years_doc and "values" in years_doc:
        all_years.update([str(y) for y in years_doc["values"]])
    
    years = sorted(list(all_years), reverse=True)
    return templates.TemplateResponse(request, "index.html", {"years": years})


@app.get("/api/makes/{year}")
async def get_makes(year: str):
    makes = await db.vehicles.distinct("make", {"year": year})
    return sorted(makes)


@app.get("/api/models/{year}/{make}")
async def get_models(year: str, make: str):
    models = await db.vehicles.distinct("model", {"year": year, "make": make})
    return sorted(models)


@app.get("/api/engines/{year}/{make}/{model}")
async def get_engines(year: str, make: str, model: str):
    engines = await db.vehicles.distinct("engine", {"year": year, "make": make, "model": model})
    return sorted(engines)


@app.get("/api/submodels/{year}/{make}/{model}/{engine}")
async def get_submodels(year: str, make: str, model: str, engine: str):
    vehicles = await db.vehicles.find(
        {"year": year, "make": make, "model": model, "engine": engine}
    ).to_list(100)
    return [{"submodel": v["submodel"], "id": str(v["_id"])} for v in vehicles]


@app.get("/vehicle/{id}")
async def vehicle_detail(request: Request, id: str):
    from bson import ObjectId
    vehicle = await db.vehicles.find_one({"_id": ObjectId(id)})
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    vehicle["_id"] = str(vehicle["_id"])
    return templates.TemplateResponse(request, "vehicle.html", {"vehicle": vehicle})


# ── Create Vehicle ──────────────────────────────────────────────────────────
@app.post("/api/vehicles")
async def create_vehicle(request: Request):
    data = await request.json()
    required = ["year", "make", "model", "engine", "submodel"]
    for f in required:
        if not data.get(f):
            raise HTTPException(status_code=400, detail=f"Field '{f}' is required")

    doc = {
        "year": data["year"].strip(),
        "make": data["make"].strip(),
        "model": data["model"].strip(),
        "engine": data["engine"].strip(),
        "submodel": data["submodel"].strip(),
        "options": data.get("options", {}),
        "timestamp": time.time(),
    }
    result = await db.vehicles.insert_one(doc)
    return {"id": str(result.inserted_id), "message": "Vehicle created"}


# ── Read Single Vehicle (JSON) ──────────────────────────────────────────────
@app.get("/api/vehicles/{vehicle_id}")
async def get_vehicle(vehicle_id: str):
    from bson import ObjectId
    v = await db.vehicles.find_one({"_id": ObjectId(vehicle_id)})
    if not v:
        raise HTTPException(status_code=404, detail="Not found")
    v["_id"] = str(v["_id"])
    return v


# ── Update Vehicle ──────────────────────────────────────────────────────────
@app.put("/api/vehicles/{vehicle_id}")
async def update_vehicle(vehicle_id: str, request: Request):
    from bson import ObjectId
    data = await request.json()
    update_doc = {}
    for field in ["year", "make", "model", "engine", "submodel"]:
        if field in data:
            update_doc[field] = data[field].strip()
    if "options" in data:
        update_doc["options"] = data["options"]

    if not update_doc:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = await db.vehicles.update_one(
        {"_id": ObjectId(vehicle_id)},
        {"$set": update_doc}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"message": "Vehicle updated"}


# ── Delete Vehicle ──────────────────────────────────────────────────────────
@app.delete("/api/vehicles/{vehicle_id}")
async def delete_vehicle(vehicle_id: str):
    from bson import ObjectId
    oid = ObjectId(vehicle_id)

    # Cascade-delete all related data
    for col in CATEGORY_MAP.values():
        await db[col].delete_many({"vehicle_id": oid})

    result = await db.vehicles.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"message": "Vehicle and all related data deleted"}


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY DATA CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/data/{category}/{vehicle_id}")
async def get_category_data(category: str, vehicle_id: str):
    from bson import ObjectId
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")

    data = await db[col_name].find({"vehicle_id": ObjectId(vehicle_id)}).to_list(1000)
    for d in data:
        d["_id"] = str(d["_id"])
        d["vehicle_id"] = str(d["vehicle_id"])
    return data


@app.post("/api/data/{category}/{vehicle_id}")
async def create_category_record(category: str, vehicle_id: str, request: Request):
    from bson import ObjectId
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")

    data = await request.json()
    data["vehicle_id"] = ObjectId(vehicle_id)
    data["timestamp"] = time.time()
    result = await db[col_name].insert_one(data)
    return {"id": str(result.inserted_id), "message": "Record created"}


@app.put("/api/data/{category}/{record_id}")
async def update_category_record(category: str, record_id: str, request: Request):
    from bson import ObjectId
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")

    data = await request.json()
    # Remove protected fields
    data.pop("_id", None)
    data.pop("vehicle_id", None)

    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = await db[col_name].update_one(
        {"_id": ObjectId(record_id)},
        {"$set": data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"message": "Record updated"}


@app.delete("/api/data/{category}/{record_id}")
async def delete_category_record(category: str, record_id: str):
    from bson import ObjectId
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")

    result = await db[col_name].delete_one({"_id": ObjectId(record_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"message": "Record deleted"}


# ─── Manual / Service Manual ─────────────────────────────────────────────────
@app.get("/api/manual/{vehicle_id}")
async def get_manual(vehicle_id: str):
    from bson import ObjectId
    manuals = await db.manuals.find({"vehicle_id": ObjectId(vehicle_id)}).to_list(1000)
    for m in manuals:
        m["_id"] = str(m["_id"])
        m["vehicle_id"] = str(m["vehicle_id"])
    return manuals


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    try:
        # Read file content
        file_content = await file.read()
        # Upload to Cloudinary
        result = cloudinary.uploader.upload(
            file_content,
            folder="carchat/vehicles",
            resource_type="auto"
        )
        return {"url": result.get("secure_url")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── HIERARCHY MANAGEMENT ──────────────────────────────────────────────

@app.put("/api/hierarchy/update")
async def update_hierarchy(data: dict):
    level = data.get("level")
    old_val = data.get("oldValue")
    new_val = data.get("newValue")
    
    query = {level: old_val}
    # Add context for nested levels
    if level in ["make", "model", "engine"]:
        query["year"] = data.get("year")
    if level in ["model", "engine"]:
        query["make"] = data.get("make")
    if level == "engine":
        query["model"] = data.get("model")
        
    # Update vehicles
    res = await db.vehicles.update_many(query, {"$set": {level: new_val}})
    
    # If it's year, also update the years collection master list
    if level == "year":
        await db.years.update_many({"values": old_val}, {"$set": {"values.$": new_val}})
        
    return {"message": f"Updated {res.modified_count} vehicles"}

@app.post("/api/hierarchy/delete")
async def delete_hierarchy(data: dict):
    level = data.get("level")
    val = data.get("value")
    
    query = {level: val}
    if level in ["make", "model", "engine"]:
        query["year"] = data.get("year")
    if level in ["model", "engine"]:
        query["make"] = data.get("make")
    if level == "engine":
        query["model"] = data.get("model")

    # Get all matching vehicle IDs to cascade delete
    vehicles = await db.vehicles.find(query).to_list(length=1000)
    v_ids = [str(v["_id"]) for v in vehicles]
    
    # Delete associated data
    for category in CATEGORY_MAP.values():
        await db[category].delete_many({"vehicle_id": {"$in": v_ids}})
        
    # Delete vehicles
    res = await db.vehicles.delete_many(query)
    
    # If it's year, also remove from master list
    if level == "year":
        await db.years.update_many({}, {"$pull": {"values": val}})
        
    return {"message": f"Deleted {res.deleted_count} vehicles and associated data"}


# ─── Scraper ─────────────────────────────────────────────────────────────────
@app.post("/api/scrape")
async def start_scrape(request: Request):
    data = await request.json()
    year = data.get("year")

    scraper_path = os.path.join(os.path.dirname(BASE_DIR), "main.py")
    cmd = [sys.executable, scraper_path]
    if year:
        cmd.extend(["--year", str(year)])

    try:
        subprocess.Popen(cmd, cwd=os.path.dirname(scraper_path))
        return {"status": "success", "message": f"Hierarchical extraction started for {year if year else 'all years'}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
