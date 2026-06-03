import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
from weasyprint import HTML, CSS
from jinja2 import Template
import aiofiles

logger = logging.getLogger(__name__)

class InvoiceService:
    def __init__(self):
        self.template_path = Path("templates/invoice.html")
        self.storage_path = Path("static/invoices")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Читаем реквизиты компании из .env
        self.company_requisites = {
            "name": os.getenv("COMPANY_NAME", "ИП Гаврин Даниил Никитич"),
            "inn": os.getenv("COMPANY_INN", "434584462396"),
            "kpp": os.getenv("COMPANY_KPP"),
            "ogrn": os.getenv("COMPANY_OGRN"),
            "address": os.getenv("COMPANY_ADDRESS", "610018, Кировская область, г. Киров, ул. Кирова 26"),
            "email": os.getenv("COMPANY_EMAIL", "daniilgavrin@bytewizard.ru"),
            "phone": os.getenv("COMPANY_PHONE"),
            "director": os.getenv("COMPANY_DIRECTOR", "Гаврин Д.Н."),
            "accountant": os.getenv("COMPANY_ACCOUNTANT"),
            "bank_name": os.getenv("COMPANY_BANK_NAME", "ПАО Сбербанк"),
            "bank_account": os.getenv("COMPANY_BANK_ACCOUNT"),
            "bank_bik": os.getenv("COMPANY_BANK_BIK"),
            "bank_corr": os.getenv("COMPANY_BANK_CORR"),
            "vat_rate": int(os.getenv("COMPANY_VAT_RATE", 0)),
        }

    async def generate_invoice_pdf(
        self,
        invoice_number: str,
        order: Dict[str, Any],
        buyer: Dict[str, Any]
    ) -> bytes:
        """Генерирует PDF счёта"""
        
        # Читаем шаблон
        with open(self.template_path, "r", encoding="utf-8") as f:
            template = Template(f.read())
        
        # Рассчитываем НДС
        vat_rate = self.company_requisites.get("vat_rate", 0)
        vat_amount = (order["total_rub"] * vat_rate / 100) if vat_rate else 0
        
        # Формируем данные для шаблона
        context = {
            "invoice_number": invoice_number,
            "date": datetime.now().strftime("%d.%m.%Y"),
            "seller": self.company_requisites,
            "buyer": buyer,
            "items": [
                {
                    "name": item["product_name"],
                    "price": item["price_rub"],
                }
                for item in order["items"]
            ],
            "total": order["total_rub"],
            "vat_rate": vat_rate if vat_rate else None,
            "vat_amount": vat_amount,
            "contract_number": "12278975",  # Можно вынести в .env
            "contract_date": "19.08.2023",
            "order_comment": order.get("client_comment"),
        }
        
        # Рендерим HTML
        html_content = template.render(**context)
        
        # Генерируем PDF
        pdf_file = HTML(string=html_content).write_pdf(
            stylesheets=[
                CSS(string='@page { size: A4; margin: 2cm; }')
            ]
        )
        
        logger.info(f"✅ Счёт {invoice_number} сгенерирован ({len(pdf_file)} байт)")
        return pdf_file

    async def save_invoice(
        self,
        invoice_number: str,
        pdf_content: bytes
    ) -> str:
        """Сохраняет PDF локально и возвращает URL"""
        filename = f"invoice_{invoice_number}.pdf"
        filepath = self.storage_path / filename
        
        async with aiofiles.open(filepath, "wb") as f:
            await f.write(pdf_content)
        
        # URL для скачивания (через эндпоинт FastAPI)
        url = f"/invoices/{filename}"
        logger.info(f"💾 Счёт сохранён локально: {filepath}")
        return url

    async def generate_invoice_number(self, db) -> str:
        """Генерирует уникальный номер счёта"""
        date_str = datetime.now().strftime("%Y%m%d")
        
        result = await db.fetchval(
            """
            SELECT MAX(invoice_number) 
            FROM invoices 
            WHERE invoice_number LIKE $1
            """,
            f"INV-{date_str}-%"
        )
        
        if result:
            last_num = int(result.split("-")[-1])
            new_num = last_num + 1
        else:
            new_num = 1
        
        invoice_number = f"INV-{date_str}-{new_num:04d}"
        return invoice_number