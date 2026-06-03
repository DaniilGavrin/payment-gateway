from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)

class InvoicePDFService:
    def __init__(self):
        # Регистрируем шрифт с поддержкой кириллицы (DejaVu Sans)
        try:
            pdfmetrics.registerFont(TTFont('DejaVuSans', 'DejaVuSans.ttf'))
            pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', 'DejaVuSans-Bold.ttf'))
            self.font_normal = 'DejaVuSans'
            self.font_bold = 'DejaVuSans-Bold'
        except:
            # Fallback если шрифты не найдены
            self.font_normal = 'Helvetica'
            self.font_bold = 'Helvetica-Bold'
            logger.warning("⚠️ DejaVu fonts not found, using Helvetica")

    def generate_invoice_pdf(
        self,
        invoice_number: str,
        order: Dict[str, Any],
        seller: Dict[str, Any],
        buyer: Dict[str, Any]
    ) -> bytes:
        """Генерирует PDF счёт и возвращает bytes"""
        
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=20*mm,
            leftMargin=20*mm,
            topMargin=20*mm,
            bottomMargin=20*mm
        )
        
        elements = []
        styles = getSampleStyleSheet()
        
        # Кастомные стили
        styles.add(ParagraphStyle(
            name='NeoPurple',
            parent=styles['Normal'],
            fontName=self.font_bold,
            fontSize=24,
            textColor=colors.HexColor('#b026ff'),
            spaceAfter=20
        ))
        
        styles.add(ParagraphStyle(
            name='SmallBold',
            parent=styles['Normal'],
            fontName=self.font_bold,
            fontSize=8,
            textColor=colors.HexColor('#8b5cf6'),
            spaceAfter=5
        ))
        
        # ==================== ШАПКА ====================
        # Логотип/Название
        elements.append(Paragraph("ByteWizard", styles['NeoPurple']))
        
        # Инфо о счёте (справа)
        header_data = [
            [f"СЧЁ № {invoice_number}", f"от {datetime.now().strftime('%d.%m.%Y')}"]
        ]
        header_table = Table(header_data, colWidths=[100*mm, 60*mm])
        header_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, -1), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, -1), 14),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#b026ff')),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 15*mm))
        
        # ==================== ПРОДАВЕЦ ====================
        elements.append(Paragraph("Продавец:", styles['SmallBold']))
        seller_info = [
            [seller.get('name', '')],
            [f"ИНН: {seller.get('inn', '')}"],
            [f"Адрес: {seller.get('address', '')}"],
            [f"Email: {seller.get('email', '')}"],
        ]
        seller_table = Table(seller_info, colWidths=[150*mm])
        seller_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.grey),
            ('PADDING', (0, 0), (-1, -1), 3),
        ]))
        elements.append(seller_table)
        elements.append(Spacer(1, 5*mm))
        
        # ==================== ПОКУПАТЕЛЬ ====================
        elements.append(Paragraph("Покупатель:", styles['SmallBold']))
        
        # Определяем имя:优先 компания, если нет — физ. лицо
        buyer_name = buyer.get('company_name') or f"{buyer.get('first_name', '')} {buyer.get('last_name', '')}".strip()
        
        buyer_info = [[buyer_name or "Клиент"]]
        
        if buyer.get('inn'):
            buyer_info.append([f"ИНН: {buyer['inn']}"])
        if buyer.get('kpp'):
            buyer_info.append([f"КПП: {buyer['kpp']}"])
        if buyer.get('legal_address'):
            buyer_info.append([f"Адрес: {buyer['legal_address']}"])
            
        buyer_info.append([f"Email: {buyer.get('email', '')}"])
        if buyer.get('phone'):
            buyer_info.append([f"Телефон: {buyer['phone']}"])
        
        buyer_table = Table(buyer_info, colWidths=[150*mm])
        buyer_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.grey),
            ('PADDING', (0, 0), (-1, -1), 3),
        ]))
        elements.append(buyer_table)
        elements.append(Spacer(1, 10*mm))
        
        # ==================== ТАБЛИЦА ТОВАРОВ ====================
        elements.append(Paragraph("Наименование услуг:", styles['SmallBold']))
        
        # Заголовки таблицы
        table_data = [[
            "№",
            "Наименование",
            "Кол-во",
            "Цена (₽)",
            "Сумма (₽)"
        ]]
        
        # Товары
        for idx, item in enumerate(order['items'], 1):
            table_data.append([
                str(idx),
                item['product_name'],
                "1",
                f"{item['price_rub']:,.2f}",
                f"{item['price_rub']:,.2f}"
            ])
        
        # Итого
        total = order['total_rub']
        table_data.append([
            "", "", "", "Итого:", f"{total:,.2f}"
        ])
        
        items_table = Table(table_data, colWidths=[15*mm, 80*mm, 20*mm, 35*mm, 35*mm])
        items_table.setStyle(TableStyle([
            # Заголовки
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#b026ff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            
            # Данные
            ('FONTNAME', (0, 1), (-1, -2), self.font_normal),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -2), 0.5, colors.grey),
            ('LINEABOVE', (-2, 0), (-1, -1), 1, colors.HexColor('#b026ff')),
            
            # Выравнивание
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Итого
            ('FONTNAME', (-2, -1), (-1, -1), self.font_bold),
            ('FONTSIZE', (-2, -1), (-1, -1), 11),
            ('TEXTCOLOR', (-1, -1), (-1, -1), colors.HexColor('#b026ff')),
            ('BACKGROUND', (-2, -1), (-1, -1), colors.HexColor('#f3e8ff')),
        ]))
        
        elements.append(items_table)
        elements.append(Spacer(1, 10*mm))
        
        # ==================== БАНКОВСКИЕ РЕКВИЗИТЫ ====================
        elements.append(Paragraph("Банковские реквизиты:", styles['SmallBold']))
        bank_info = [
            [f"Банк: {seller.get('bank_name', '')}"],
            [f"Расчётный счёт: {seller.get('bank_account', '')}"],
            [f"БИК: {seller.get('bank_bik', '')}"],
            [f"Корр. счёт: {seller.get('bank_corr', '')}"] if seller.get('bank_corr') else [],
        ]
        bank_table = Table(bank_info, colWidths=[150*mm])
        bank_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f9f9f9')),
            ('PADDING', (0, 0), (-1, -1), 5),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(bank_table)
        
        # ==================== ПОДВАЛ ====================
        elements.append(Spacer(1, 15*mm))
        footer_text = f"""
        <b>Руководитель</b> ____________________ / {seller.get('director', '')} /<br/><br/>
        <b>Главный бухгалтер</b> ____________________ / {seller.get('accountant', '____________') or '____________'} /
        """
        elements.append(Paragraph(footer_text, styles['Normal']))
        
        elements.append(Spacer(1, 10*mm))
        elements.append(Paragraph(
            "<i>Счёт действителен в течение 3 банковских дней.</i>",
            ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, textColor=colors.grey)
        ))
        
        # ==================== ГЕНЕРАЦИЯ ====================
        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        logger.info(f"✅ PDF сгенерирован: {invoice_number} ({len(pdf_bytes)} байт)")
        return pdf_bytes