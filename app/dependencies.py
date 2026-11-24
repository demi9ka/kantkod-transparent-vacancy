from typing import Annotated

from fastapi import Depends, Request

from app.services.gigachat import GigaChatClient
from app.services.reports import ReportService


def get_report_service(request: Request) -> ReportService:
    return request.app.state.report_service


def get_gigachat_client(request: Request) -> GigaChatClient:
    return request.app.state.gigachat_client


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]
GigaChatDep = Annotated[GigaChatClient, Depends(get_gigachat_client)]
