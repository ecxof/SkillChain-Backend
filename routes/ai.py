from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def ai_root():
    return {"message": "AI route"}