from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def badges_root():
    return {"message": "Badges route"}