from fastapi import APIRouter, UploadFile, File, HTTPException
import cloudinary
import cloudinary.uploader

router = APIRouter(prefix="/api")

@router.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    try:
        file_content = await file.read()
        result = cloudinary.uploader.upload(
            file_content,
            folder="carchat/vehicles",
            resource_type="auto"
        )
        return {"url": result.get("secure_url")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
