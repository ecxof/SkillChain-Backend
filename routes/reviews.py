from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def reviews_root():
    return {"message": "Reviews route"}