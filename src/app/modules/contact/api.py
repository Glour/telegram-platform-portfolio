from fastapi import APIRouter

from app.modules.contact.schemas import ContactRequest, ContactResponse
from app.modules.contact.service import send_contact_request

router = APIRouter()


@router.post("", response_model=ContactResponse)
async def create_contact_request(payload: ContactRequest) -> ContactResponse:
    await send_contact_request(payload)
    return ContactResponse(status="ok", message="Сообщение отправлено. Мы свяжемся с вами.")
