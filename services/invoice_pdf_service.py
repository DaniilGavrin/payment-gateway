import qrcode
from qrcode.image.styledpil import StyledPilImage
from num2words import num2words
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import logging
import os
from datetime import datetime
from typing import Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)


class InvoicePDFService:
    def __init__(self):
        # По умолчанию используем стандартные шрифты
        self.font_normal = 'Helvetica'
        self.font_bold = 'Helvetica-Bold'
        
        # 🔹 Пытаемся зарегистрировать шрифты DejaVu для кириллицы
        try:
            base_dir = Path(__file__).parent.parent
            fonts_dir = base_dir / "fonts"
            
            regular_font = fonts_dir / "DejaVuSans.ttf"
            bold_font = fonts_dir / "DejaVuSans-Bold.ttf"
            
            if regular_font.exists() and bold_font.exists():
                pdfmetrics.registerFont(TTFont('DejaVuSans', str(regular_font)))
                pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', str(bold_font)))
                self.font_normal = 'DejaVuSans'
                self.font_bold = 'DejaVuSans-Bold'
                logger.info("✅ DejaVu fonts loaded successfully")
            else:
                logger.warning(f"⚠️ DejaVu fonts not found in {fonts_dir}")
                logger.warning("⚠️ Using Helvetica (Cyrillic will NOT work)")
        except Exception as e:
            logger.error(f"❌ Font loading error: {e}")
            logger.error("⚠️ Using Helvetica (Cyrillic will NOT work)")

    def _create_qr_code(self, seller: Dict[str, Any], total: float) -> BytesIO:
        """
        Создаёт QR-код для быстрой оплаты по банковским реквизитам.
        Формат: ST00012 (ГОСТ Р 56042-2014) или просто текст с реквизитами
        """
        # Формируем строку для QR-кода
        # Можно использовать формат ST00012 для банковских приложений
        qr_data = f"""Name:{seller.get('name', '')}
PersonalAcc:{seller.get('bank_account', '')}
BankName:{seller.get('bank_name', '')}
BIC:{seller.get('bank_bik', '')}
CorrespAcc:{seller.get('bank_corr', '')}
Sum:{int(total * 100)}
Purpose:Оплата по счёту
PayeeINN:{seller.get('inn', '')}"""
        
        # Создаём QR-код
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        # Сохраняем в BytesIO
        img_buffer = BytesIO()
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        
        return img_buffer

    def _format_amount_words(self, amount: float) -> str:
        """Форматирует сумму прописью"""
        try:
            # Разделяем на рубли и копейки
            rubles = int(amount)
            kopecks = int((amount - rubles) * 100)
            
            # Форматируем рубли прописью
            rubles_words = num2words(rubles, lang='ru', to='cardinal').capitalize()
            
            # Форматируем полное сообщение
            result = f"{rubles_words} рублей {kopecks:02d} копеек"
            
            return result
        except Exception as e:
            logger.error(f"Ошибка форматирования суммы прописью: {e}")
            return f"{amount:.2f} руб."

    def generate_invoice_pdf(
        self,
        invoice_number: str,
        order: Dict[str, Any],
        seller: Dict[str, Any],
        buyer: Dict[str, Any]
    ) -> bytes:
        """Генерирует PDF счёт в стиле Reg.ru и возвращает bytes"""
        
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=15*mm,
            leftMargin=15*mm,
            topMargin=15*mm,
            bottomMargin=15*mm
        )
        
        elements = []
        styles = getSampleStyleSheet()
        
        # ==================== ШАПКА ====================
        # Левая часть: название компании
        left_header = []
        left_header.append(Paragraph(
            seller.get('name', 'ИП'),
            ParagraphStyle(
                'CompanyName',
                parent=styles['Normal'],
                fontName=self.font_bold,
                fontSize=14,
                textColor=colors.black,
                spaceAfter=4
            )
        ))
        
        # ИНН/КПП/ОГРН
        inn_text = f"ИНН {seller.get('inn', '')}"
        if seller.get('kpp'):
            inn_text += f" / КПП {seller.get('kpp')}"
        left_header.append(Paragraph(
            inn_text,
            ParagraphStyle('INN', parent=styles['Normal'], fontSize=9, textColor=colors.black)
        ))
        
        if seller.get('ogrn'):
            left_header.append(Paragraph(
                f"ОГРН {seller.get('ogrn')}",
                ParagraphStyle('OGRN', parent=styles['Normal'], fontSize=9, textColor=colors.black)
            ))
        
        if seller.get('address'):
            left_header.append(Paragraph(
                seller.get('address'),
                ParagraphStyle('Address', parent=styles['Normal'], fontSize=9, textColor=colors.black)
            ))
        
        # Правая часть: СЧЁТ
        right_header = []
        right_header.append(Paragraph(
            f"Счёт № {invoice_number} от {datetime.now().strftime('%d.%m.%Y')}",
            ParagraphStyle(
                'InvoiceNumber',
                parent=styles['Normal'],
                fontName=self.font_bold,
                fontSize=14,
                textColor=colors.black,
                alignment=TA_RIGHT,
                spaceAfter=8
            )
        ))
        
        # Создаём таблицу для шапки
        header_table = Table(
            [[left_header, right_header]],
            colWidths=[100*mm, 80*mm]
        )
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 15*mm))
        
        # ==================== ИСПОЛНИТЕЛЬ ====================
        elements.append(Paragraph(
            "<b>Исполнитель:</b>",
            ParagraphStyle('SectionTitle', parent=styles['Normal'], fontSize=10, fontName=self.font_bold)
        ))
        
        contractor_info = []
        contractor_info.append([seller.get('name', 'Не указано')])
        if seller.get('inn'):
            contractor_info.append([f"ИНН: {seller['inn']}"])
        if seller.get('kpp'):
            contractor_info.append([f"КПП: {seller['kpp']}"])
        if seller.get('address'):
            contractor_info.append([f"Адрес: {seller['address']}"])
        if seller.get('email'):
            contractor_info.append([f"Email: {seller['email']}"])
        if seller.get('phone'):
            contractor_info.append([f"Телефон: {seller['phone']}"])
        
        contractor_table = Table(contractor_info, colWidths=[170*mm])
        contractor_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('PADDING', (0, 0), (-1, -1), 2),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.black),
        ]))
        elements.append(contractor_table)
        elements.append(Spacer(1, 8*mm))
        
        # ==================== ЗАКАЗЧИК ====================
        elements.append(Paragraph(
            "<b>Заказчик:</b>",
            ParagraphStyle('SectionTitle2', parent=styles['Normal'], fontSize=10, fontName=self.font_bold)
        ))
        
        # Определяем имя заказчика
        customer_name = buyer.get('company_name')
        if not customer_name:
            first = buyer.get('first_name') or ''
            last = buyer.get('last_name') or ''
            customer_name = f"{last} {first}".strip() or "Клиент"
        
        customer_info = [[customer_name]]
        if buyer.get('inn'):
            customer_info.append([f"ИНН: {buyer['inn']}"])
        if buyer.get('kpp'):
            customer_info.append([f"КПП: {buyer['kpp']}"])
        if buyer.get('legal_address'):
            customer_info.append([f"Адрес: {buyer['legal_address']}"])
        if buyer.get('email'):
            customer_info.append([f"Email: {buyer['email']}"])
        if buyer.get('phone'):
            customer_info.append([f"Телефон: {buyer['phone']}"])
        
        customer_table = Table(customer_info, colWidths=[170*mm])
        customer_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('PADDING', (0, 0), (-1, -1), 2),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.black),
        ]))
        elements.append(customer_table)
        elements.append(Spacer(1, 8*mm))
        
        # ==================== ОСНОВАНИЕ ====================
        contract_number = buyer.get('contract_number', 'Д-1')
        contract_date = buyer.get('contract_date', datetime.now().strftime('%d.%m.%Y'))
        contract_url = os.getenv('CONTRACT_URL', 'https://bytewizard.ru/offer')
        
        elements.append(Paragraph(
            f"<b>Основание:</b> Договор № {contract_number} от {contract_date}",
            ParagraphStyle('Basis', parent=styles['Normal'], fontSize=10, spaceAfter=10)
        ))
        elements.append(Spacer(1, 5*mm))
        
        # ==================== ТАБЛИЦА ТОВАРОВ ====================
        # Заголовки таблицы
        table_data = [[
            Paragraph("<b>№</b>", ParagraphStyle('TH', parent=styles['Normal'], fontSize=9, fontName=self.font_bold)),
            Paragraph("<b>Товары (работы, услуги)</b>", ParagraphStyle('TH', parent=styles['Normal'], fontSize=9, fontName=self.font_bold)),
            Paragraph("<b>Кол-во</b>", ParagraphStyle('TH', parent=styles['Normal'], fontSize=9, fontName=self.font_bold)),
            Paragraph("<b>Ед.</b>", ParagraphStyle('TH', parent=styles['Normal'], fontSize=9, fontName=self.font_bold)),
            Paragraph("<b>Цена</b>", ParagraphStyle('TH', parent=styles['Normal'], fontSize=9, fontName=self.font_bold)),
            Paragraph("<b>Сумма</b>", ParagraphStyle('TH', parent=styles['Normal'], fontSize=9, fontName=self.font_bold)),
        ]]
        
        # Товары
        for idx, item in enumerate(order['items'], 1):
            table_data.append([
                Paragraph(str(idx), ParagraphStyle('TD', parent=styles['Normal'], fontSize=9)),
                Paragraph(item['product_name'], ParagraphStyle('TD', parent=styles['Normal'], fontSize=9)),
                Paragraph("1", ParagraphStyle('TD', parent=styles['Normal'], fontSize=9, alignment=TA_CENTER)),
                Paragraph("шт", ParagraphStyle('TD', parent=styles['Normal'], fontSize=9, alignment=TA_CENTER)),
                Paragraph(f"{item['price_rub']:,.2f}", ParagraphStyle('TD', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT)),
                Paragraph(f"{item['price_rub']:,.2f}", ParagraphStyle('TD', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT)),
            ])
        
        # Итого
        total = order['total_rub']
        vat_rate = buyer.get('vat_rate', 20)  # По умолчанию 20%
        vat_amount = total * vat_rate / 120 if vat_rate > 0 else 0
        
        table_data.append([
            "", "", "", "",
            Paragraph("<b>Итого:</b>", ParagraphStyle('Total', parent=styles['Normal'], fontSize=9, fontName=self.font_bold, alignment=TA_RIGHT)),
            Paragraph(f"<b>{total:,.2f}</b>", ParagraphStyle('Total', parent=styles['Normal'], fontSize=9, fontName=self.font_bold, alignment=TA_RIGHT)),
        ])
        
        if vat_rate > 0:
            table_data.append([
                "", "", "", "",
                Paragraph(f"В т.ч. НДС {vat_rate}%:", ParagraphStyle('VAT', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT)),
                Paragraph(f"{vat_amount:.2f}", ParagraphStyle('VAT', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT)),
            ])
        
        table_data.append([
            "", "", "", "",
            Paragraph("<b>Всего к оплате:</b>", ParagraphStyle('GrandTotal', parent=styles['Normal'], fontSize=9, fontName=self.font_bold, alignment=TA_RIGHT)),
            Paragraph(f"<b>{total:,.2f}</b>", ParagraphStyle('GrandTotal', parent=styles['Normal'], fontSize=9, fontName=self.font_bold, alignment=TA_RIGHT)),
        ])
        
        items_table = Table(table_data, colWidths=[15*mm, 85*mm, 20*mm, 20*mm, 25*mm, 25*mm])
        items_table.setStyle(TableStyle([
            # Заголовки
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f5f5f5')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('FONTNAME', (0, 0), (-1, 0), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            
            # Границы
            ('GRID', (0, 0), (-1, -3), 0.5, colors.black),
            ('LINEBELOW', (0, -3), (-1, -1), 0.5, colors.black),
            
            # Выравнивание
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('ALIGN', (2, 0), (3, -1), 'CENTER'),
            ('ALIGN', (4, 0), (-1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Отступы в ячейках
            ('PADDING', (0, 1), (-1, -1), 6),
        ]))
        
        elements.append(items_table)
        elements.append(Spacer(1, 10*mm))
        
        # ==================== СУММА ПРОПИСЬЮ ====================
        items_count = len(order['items'])
        amount_words = self._format_amount_words(total)
        
        total_text = f"""
<b>Всего наименований {items_count}, на сумму {total:,.2f} рублей</b><br/>
<b>{amount_words.capitalize()}</b>
""".strip()
        
        elements.append(Paragraph(
            total_text,
            ParagraphStyle('TotalWords', parent=styles['Normal'], fontSize=10, spaceAfter=10)
        ))
        elements.append(Spacer(1, 10*mm))
        
        # ==================== БАНКОВСКИЕ РЕКВИЗИТЫ С QR-КОДОМ ====================
        elements.append(Paragraph(
            "<b>Банковские реквизиты:</b>",
            ParagraphStyle('BankTitle', parent=styles['Normal'], fontSize=10, fontName=self.font_bold, spaceAfter=8)
        ))
        
        # Левая часть: реквизиты
        bank_info = []
        if seller.get('bank_name'):
            bank_info.append([f"Банк: {seller['bank_name']}"])
        if seller.get('bank_account'):
            bank_info.append([f"Расчётный счёт: {seller['bank_account']}"])
        if seller.get('bank_bik'):
            bank_info.append([f"БИК: {seller['bank_bik']}"])
        if seller.get('bank_corr'):
            bank_info.append([f"Корр. счёт: {seller['bank_corr']}"])
        
        # Правая часть: QR-код
        try:
            qr_buffer = self._create_qr_code(seller, total)
            qr_img = Image(qr_buffer, width=40*mm, height=40*mm)
            bank_table = Table(
                [[bank_info, qr_img]],
                colWidths=[130*mm, 50*mm]
            )
            bank_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ]))
            elements.append(bank_table)
        except Exception as e:
            logger.error(f"Ошибка создания QR-кода: {e}")
            # Если QR не получился, просто выводим реквизиты
            bank_table = Table(bank_info, colWidths=[170*mm])
            bank_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f9f9f9')),
                ('PADDING', (0, 0), (-1, -1), 5),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.grey),
            ]))
            elements.append(bank_table)
        
        elements.append(Spacer(1, 15*mm))
        
        # ==================== FOOTER С ОФЕРТОЙ ====================
        offer_url = os.getenv('OFFER_URL', 'https://bytewizard.ru/offer')
        
        footer_text = f"""
Оплата данного счёта означает согласие Заказчика с условиями Договора об оказании услуг (далее - Договор),
являющегося публичной офертой и размещенного по адресу {offer_url}. В дальнейшем Заказчик осуществляет 
оплату услуг по Договору в соответствии с условиями Договора.
        """.strip()
        
        elements.append(Paragraph(
            footer_text,
            ParagraphStyle(
                'OfferFooter',
                parent=styles['Normal'],
                fontSize=8,
                textColor=colors.grey,
                alignment=TA_LEFT,
                leading=10
            )
        ))
        
        # ==================== ГЕНЕРАЦИЯ ====================
        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        logger.info(f"✅ PDF сгенерирован: {invoice_number} ({len(pdf_bytes)} байт)")
        return pdf_bytes