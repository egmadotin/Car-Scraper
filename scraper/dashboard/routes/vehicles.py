from fastapi import APIRouter, Request, HTTPException
from bson import ObjectId
from ..db import db, CATEGORY_MAP

router = APIRouter(prefix="/api")

@router.get("/makes/{year}")
async def get_makes(year: str):
    makes = await db.vehicles.distinct("make", {"year": year})
    return sorted(makes)

@router.get("/models/{year}/{make}")
async def get_models(year: str, make: str):
    models = await db.vehicles.distinct("model", {"year": year, "make": make})
    return sorted(models)

@router.get("/engines/{year}/{make}/{model}")
async def get_engines(year: str, make: str, model: str):
    engines = await db.vehicles.distinct("engine", {"year": year, "make": make, "model": model})
    return sorted(engines)

@router.get("/submodels/{year}/{make}/{model}/{engine}")
async def get_submodels(year: str, make: str, model: str, engine: str):
    vehicles = await db.vehicles.find({"year": year, "make": make, "model": model, "engine": engine}).to_list(100)
    for v in vehicles:
        v["id"] = str(v["_id"])
        del v["_id"]
    return vehicles

@router.post("/vehicles")
async def create_vehicle(request: Request):
    data = await request.json()
    doc = {
        "year": data.get("year", "").strip(),
        "make": data.get("make", "").strip(),
        "model": data.get("model", "").strip(),
        "engine": data.get("engine", "").strip(),
        "submodel": data.get("submodel", "").strip(),
        "options": []
    }
    if not all([doc["year"], doc["make"], doc["model"], doc["engine"], doc["submodel"]]):
        raise HTTPException(status_code=400, detail="Missing required fields")
    
    result = await db.vehicles.insert_one(doc)
    return {"id": str(result.inserted_id), "message": "Vehicle created"}

@router.get("/vehicles/{vehicle_id}")
async def get_vehicle(vehicle_id: str):
    v = await db.vehicles.find_one({"_id": ObjectId(vehicle_id)})
    if not v:
        raise HTTPException(status_code=404, detail="Not found")
    v["_id"] = str(v["_id"])
    return v

@router.put("/vehicles/{vehicle_id}")
async def update_vehicle(vehicle_id: str, request: Request):
    data = await request.json()
    update_doc = {}
    for field in ["year", "make", "model", "engine", "submodel"]:
        if field in data:
            update_doc[field] = data[field].strip()
    if "options" in data:
        update_doc["options"] = data["options"]

    if not update_doc:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = await db.vehicles.update_one({"_id": ObjectId(vehicle_id)}, {"$set": update_doc})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"message": "Vehicle updated"}

@router.delete("/vehicles/{vehicle_id}")
async def delete_vehicle(vehicle_id: str):
    v_id = ObjectId(vehicle_id)
    # Cascade delete
    for category in CATEGORY_MAP.values():
        await db[category].delete_many({"vehicle_id": v_id})
    
    res = await db.vehicles.delete_one({"_id": v_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"message": "Vehicle and all associated data deleted"}

# ── HIERARCHY MANAGEMENT ──────────────────────────────────────────────

@router.put("/hierarchy/update")
async def update_hierarchy(data: dict):
    level = data.get("level")
    old_val = data.get("oldValue")
    new_val = data.get("newValue")
    
    query = {level: old_val}
    if level in ["make", "model", "engine"]: query["year"] = data.get("year")
    if level in ["model", "engine"]: query["make"] = data.get("make")
    if level == "engine": query["model"] = data.get("model")
        
    res = await db.vehicles.update_many(query, {"$set": {level: new_val}})
    if level == "year":
        await db.years.update_many({"values": old_val}, {"$set": {"values.$": new_val}})
        
    return {"message": f"Updated {res.modified_count} vehicles"}

@router.post("/hierarchy/delete")
async def delete_hierarchy(data: dict):
    level = data.get("level")
    val = data.get("value")
    
    query = {level: val}
    if level in ["make", "model", "engine"]: query["year"] = data.get("year")
    if level in ["model", "engine"]: query["make"] = data.get("make")
    if level == "engine": query["model"] = data.get("model")

    vehicles = await db.vehicles.find(query).to_list(length=1000)
    v_ids = [v["_id"] for v in vehicles]
    
    for category in CATEGORY_MAP.values():
        await db[category].delete_many({"vehicle_id": {"$in": v_ids}})
        
    res = await db.vehicles.delete_many(query)
    if level == "year":
        await db.years.update_many({}, {"$pull": {"values": val}})
        
    return {"message": f"Deleted {res.deleted_count} vehicles and associated data"}
