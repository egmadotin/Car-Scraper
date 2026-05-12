from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
import uvicorn
import secrets
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Depends, HTTPException, status
try:
    from .db import db
    from .routes import vehicles, data, upload
except ImportError:
    from db import db
    from routes import vehicles, data, upload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Configure Cloudinary
import cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

security = HTTPBasic()

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = os.getenv("DASHBOARD_USERNAME", "Egma")
    correct_password = os.getenv("DASHBOARD_PASSWORD", "Carscraper@egma")
    
    is_correct_username = secrets.compare_digest(credentials.username, correct_username)
    is_correct_password = secrets.compare_digest(credentials.password, correct_password)
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

app = FastAPI(
    title="Vehicle Hierarchy Explorer",
    dependencies=[Depends(authenticate)]
)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Include modular routes
app.include_router(vehicles.router)
app.include_router(data.router)
app.include_router(upload.router)

@app.get("/")
async def home(request: Request):
    # Fetch years from master list
    years_doc = await db.years.find_one({})
    master_years = set(years_doc.get("values", []) if years_doc else [])
    
    # Also fetch unique years currently in the vehicles collection (backup)
    vehicle_years = await db.vehicles.distinct("year")
    
    # Combine and sort
    all_years = sorted(list(master_years.union(set(vehicle_years))), reverse=True)
    
    return templates.TemplateResponse(request, "index.html", {"years": all_years})

@app.get("/vehicle/{vehicle_id}")
async def vehicle_detail(request: Request, vehicle_id: str):
    from bson import ObjectId
    v = await db.vehicles.find_one({"_id": ObjectId(vehicle_id)})
    if not v:
        return "Vehicle not found", 404
    v["_id"] = str(v["_id"])
    return templates.TemplateResponse(request, "vehicle.html", {"vehicle": v})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081)
