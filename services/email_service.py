import os
import logging
import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

logger = logging.getLogger(__name__)

class EmailService:
    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("FROM_EMAIL", "noreply@bytewizard.ru")

    async def send_invoice_email(
        self,
        to_email: str,
        invoice_number: str,
        pdf_content: bytes,
        total_rub: float
    ):
        """Отправляет счёт на email с PDF-вложением"""
        
        if not self.smtp_host:
            logger.warning("⚠️ SMTP не настроен, email не отправлен")
            return
        
        subject = f"Счёт № {invoice_number} от ByteWizard"
        
        body = f"""
Здравствуйте!

Благодарим за заказ в ByteWizard.

Во вложении вы найдёте счёт № {invoice_number} на сумму {total_rub:,.2f} ₽.

Реквизиты для оплаты указаны в счёте.
После оплаты мы свяжемся с вами для уточнения деталей.

С уважением,
Команда ByteWizard
https://bytewizard.ru
        """.strip()
        
        msg = MIMEMultipart()
        msg["From"] = self.from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        # Прикрепляем PDF
        attachment = MIMEBase("application", "octet-stream")
        attachment.set_payload(pdf_content)
        encoders.encode_base64(attachment)
        attachment.add_header(
            "Content-Disposition",
            f"attachment; filename=invoice_{invoice_number}.pdf"
        )
        msg.attach(attachment)
        
        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                start_tls=True
            )
            logger.info(f"✅ Счёт {invoice_number} отправлен на {to_email}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки email: {e}")
            raise