from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import logging
import os
from datetime import datetime
from typing import Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class InvoicePDFService:
    def __init__(self):
        # По умолчанию используем стандартные шрифты
        self.font_normal = 'Helvetica'
        self.font_bold = 'Helvetica-Bold'
        
        # 🔹 Пытаемся зарегистрировать шрифты DejaVu для кириллицы
        try:
            # Ищем шрифты в папке fonts/
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
        
        # 🔹 Кастомные стили с неоновыми цветами
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
            fontSize=9,
            textColor=colors.HexColor('#8b5cf6'),
            spaceAfter=6
        ))
        
        # ==================== ШАПКА ====================
        elements.append(Paragraph("ByteWizard", styles['NeoPurple']))
        
        # Инфо о счёте (справа)
        header_data = [
            [f"СЧЁТ № {invoice_number}", f"от {datetime.now().strftime('%d.%m.%Y')}"]
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
            [seller.get('name') or "Не указано"],
        ]
        if seller.get('inn'):
            seller_info.append([f"ИНН: {seller['inn']}"])
        if seller.get('ogrn'):
            seller_info.append([f"ОГРН: {seller['ogrn']}"])
        if seller.get('address'):
            seller_info.append([f"Адрес: {seller['address']}"])
        if seller.get('email'):
            seller_info.append([f"Email: {seller['email']}"])
        if seller.get('phone'):
            seller_info.append([f"Тел: {seller['phone']}"])
        
        seller_table = Table(seller_info, colWidths=[150*mm])
        seller_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.grey),
            ('PADDING', (0, 0), (-1, -1), 3),
        ]))
        elements.append(seller_table)
        elements.append(Spacer(1, 8*mm))
        
        # ==================== ПОКУПАТЕЛЬ ====================
        elements.append(Paragraph("Покупатель:", styles['SmallBold']))
        
        # 🔹 ИСПРАВЛЕНО: Корректное определение имени покупателя
        buyer_name = buyer.get('company_name')
        if not buyer_name:
            first = buyer.get('first_name') or ''
            last = buyer.get('last_name') or ''
            buyer_name = f"{last} {first}".strip() or "Клиент"
        
        buyer_info = [[buyer_name]]
        
        if buyer.get('inn'):
            buyer_info.append([f"ИНН: {buyer['inn']}"])
        if buyer.get('kpp'):
            buyer_info.append([f"КПП: {buyer['kpp']}"])
        if buyer.get('legal_address'):
            buyer_info.append([f"Адрес: {buyer['legal_address']}"])
        if buyer.get('email'):
            buyer_info.append([f"Email: {buyer['email']}"])
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
        bank_info = []
        
        if seller.get('bank_name'):
            bank_info.append([f"Банк: {seller['bank_name']}"])
        if seller.get('bank_account'):
            bank_info.append([f"Расчётный счёт: {seller['bank_account']}"])
        if seller.get('bank_bik'):
            bank_info.append([f"БИК: {seller['bank_bik']}"])
        if seller.get('bank_corr'):
            bank_info.append([f"Корр. счёт: {seller['bank_corr']}"])
        
        # 🔹 Если реквизиты есть — рисуем таблицу
        if bank_info:
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
        elements.append(Spacer(1, 20*mm))
        
        footer_style = ParagraphStyle(
            'FooterStyle',
            parent=styles['Normal'],
            fontName=self.font_normal,
            fontSize=9
        )
        
        director_name = seller.get('director') or "____________"
        accountant_name = seller.get('accountant') or "____________"
        
        footer_text = f"""
        <b>Руководитель</b> ____________________ / {director_name} /<br/><br/>
        <b>Главный бухгалтер</b> ____________________ / {accountant_name} /
        """
        elements.append(Paragraph(footer_text, footer_style))
        
        elements.append(Spacer(1, 10*mm))
        
        footer_note = ParagraphStyle(
            'FooterNote',
            parent=styles['Normal'],
            fontName=self.font_normal,
            fontSize=8,
            textColor=colors.grey
        )
        elements.append(Paragraph(
            "<i>Счёт действителен в течение 3 банковских дней.</i>",
            footer_note
        ))
        
        # ==================== ГЕНЕРАЦИЯ ====================
        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        logger.info(f"✅ PDF сгенерирован: {invoice_number} ({len(pdf_bytes)} байт)")
        return pdf_bytes