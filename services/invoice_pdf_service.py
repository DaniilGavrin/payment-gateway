from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
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
        self.font_normal = 'Helvetica'
        self.font_bold = 'Helvetica-Bold'
        self._load_fonts()

    def _load_fonts(self):
        """Пытается загрузить кириллические шрифты с системных путей"""
        
        # Пути к шрифтам на Linux (Vercel/AWS Lambda)
        font_paths = [
            # DejaVu Sans
            ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 
             '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
            ('/usr/share/fonts/dejavu/DejaVuSans.ttf',
             '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'),
            ('/usr/share/fonts/TTF/DejaVuSans.ttf',
             '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'),
            # Liberation Sans (аналог Arial)
            ('/usr/share/fonts/liberation/LiberationSans-Regular.ttf',
             '/usr/share/fonts/liberation/LiberationSans-Bold.ttf'),
            # GNU FreeFont
            ('/usr/share/fonts/truetype/freefont/FreeSans.ttf',
             '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf'),
        ]
        
        for reg_path, bold_path in font_paths:
            if Path(reg_path).exists() and Path(bold_path).exists():
                try:
                    pdfmetrics.registerFont(TTFont('CustomSans', reg_path))
                    pdfmetrics.registerFont(TTFont('CustomSansBold', bold_path))
                    self.font_normal = 'CustomSans'
                    self.font_bold = 'CustomSansBold'
                    logger.info(f"✅ Шрифты загружены: {reg_path}")
                    return
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось загрузить шрифты из {reg_path}: {e}")
                    continue
        
        # Если ничего не найдено - используем стандартные (кириллица НЕ будет работать)
        logger.warning("⚠️ Кириллические шрифты не найдены! Используем Helvetica (кириллица может не отображаться)")

    def _create_qr_code(self, seller: Dict[str, Any], total: float) -> BytesIO:
        """Создаёт QR-код для быстрой оплаты"""
        import qrcode
        
        qr_data = f"""Name:{seller.get('name', '')}
PersonalAcc:{seller.get('bank_account', '')}
BankName:{seller.get('bank_name', '')}
BIC:{seller.get('bank_bik', '')}
CorrespAcc:{seller.get('bank_corr', '')}
Sum:{int(total * 100)}
Purpose:Оплата по счёту
PayeeINN:{seller.get('inn', '')}"""
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        img_buffer = BytesIO()
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        
        return img_buffer

    def _format_amount_words(self, amount: float) -> str:
        """Форматирует сумму прописью"""
        try:
            from num2words import num2words
            rubles = int(amount)
            kopecks = int((amount - rubles) * 100)
            rubles_words = num2words(rubles, lang='ru', to='cardinal').capitalize()
            return f"{rubles_words} рублей {kopecks:02d} копеек"
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
        """Генерирует PDF счёт в стиле Reg.ru"""
        
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
        
        # Стили для ячеек
        styles.add(ParagraphStyle(
            'CellNormal',
            parent=styles['Normal'],
            fontName=self.font_normal,
            fontSize=9,
            textColor=colors.black,
            leading=11
        ))
        
        styles.add(ParagraphStyle(
            'CellBold',
            parent=styles['Normal'],
            fontName=self.font_bold,
            fontSize=9,
            textColor=colors.black,
            leading=11
        ))
        
        # ==================== ШАПКА ====================
        left_header = []
        left_header.append(Paragraph(
            seller.get('name', 'ИП'),
            ParagraphStyle('CompanyName', parent=styles['Normal'], fontName=self.font_bold, fontSize=14, spaceAfter=4)
        ))
        
        inn_text = f"ИНН {seller.get('inn', '')}"
        if seller.get('kpp'):
            inn_text += f" / КПП {seller.get('kpp')}"
        left_header.append(Paragraph(inn_text, styles['CellNormal']))
        
        if seller.get('ogrn'):
            left_header.append(Paragraph(f"ОГРН {seller.get('ogrn')}", styles['CellNormal']))
        
        if seller.get('address'):
            left_header.append(Paragraph(seller.get('address'), styles['CellNormal']))
        
        right_header = []
        right_header.append(Paragraph(
            f"Счёт № {invoice_number} от {datetime.now().strftime('%d.%m.%Y')}",
            ParagraphStyle('InvoiceNumber', parent=styles['Normal'], fontName=self.font_bold, fontSize=14, alignment=TA_RIGHT, spaceAfter=8)
        ))
        
        header_table = Table([[left_header, right_header]], colWidths=[100*mm, 80*mm])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 15*mm))
        
        # ==================== ИСПОЛНИТЕЛЬ ====================
        elements.append(Paragraph("<b>Исполнитель:</b>", ParagraphStyle('SectionTitle', parent=styles['Normal'], fontSize=10, fontName=self.font_bold)))
        
        contractor_info = []
        contractor_info.append([Paragraph(seller.get('name', 'Не указано'), styles['CellNormal'])])
        if seller.get('inn'):
            contractor_info.append([Paragraph(f"ИНН: {seller['inn']}", styles['CellNormal'])])
        if seller.get('kpp'):
            contractor_info.append([Paragraph(f"КПП: {seller['kpp']}", styles['CellNormal'])])
        if seller.get('address'):
            contractor_info.append([Paragraph(f"Адрес: {seller['address']}", styles['CellNormal'])])
        if seller.get('email'):
            contractor_info.append([Paragraph(f"Email: {seller['email']}", styles['CellNormal'])])
        if seller.get('phone'):
            contractor_info.append([Paragraph(f"Телефон: {seller['phone']}", styles['CellNormal'])])
        
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
        elements.append(Paragraph("<b>Заказчик:</b>", ParagraphStyle('SectionTitle2', parent=styles['Normal'], fontSize=10, fontName=self.font_bold)))
        
        customer_name = buyer.get('company_name')
        if not customer_name:
            first = buyer.get('first_name') or ''
            last = buyer.get('last_name') or ''
            customer_name = f"{last} {first}".strip() or "Клиент"
        
        customer_info = [[Paragraph(customer_name, styles['CellNormal'])]]
        if buyer.get('inn'):
            customer_info.append([Paragraph(f"ИНН: {buyer['inn']}", styles['CellNormal'])])
        if buyer.get('kpp'):
            customer_info.append([Paragraph(f"КПП: {buyer['kpp']}", styles['CellNormal'])])
        if buyer.get('legal_address'):
            customer_info.append([Paragraph(f"Адрес: {buyer['legal_address']}", styles['CellNormal'])])
        if buyer.get('email'):
            customer_info.append([Paragraph(f"Email: {buyer['email']}", styles['CellNormal'])])
        if buyer.get('phone'):
            customer_info.append([Paragraph(f"Телефон: {buyer['phone']}", styles['CellNormal'])])
        
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
        
        elements.append(Paragraph(
            f"<b>Основание:</b> Договор № {contract_number} от {contract_date}",
            ParagraphStyle('Basis', parent=styles['Normal'], fontSize=10, spaceAfter=10)
        ))
        elements.append(Spacer(1, 5*mm))
        
        # ==================== ТАБЛИЦА ТОВАРОВ ====================
        table_data = [[
            Paragraph("<b>№</b>", styles['CellBold']),
            Paragraph("<b>Товары (работы, услуги)</b>", styles['CellBold']),
            Paragraph("<b>Кол-во</b>", styles['CellBold']),
            Paragraph("<b>Ед.</b>", styles['CellBold']),
            Paragraph("<b>Цена</b>", styles['CellBold']),
            Paragraph("<b>Сумма</b>", styles['CellBold']),
        ]]
        
        for idx, item in enumerate(order['items'], 1):
            table_data.append([
                Paragraph(str(idx), styles['CellNormal']),
                Paragraph(item['product_name'], styles['CellNormal']),
                Paragraph("1", ParagraphStyle('Center', parent=styles['CellNormal'], alignment=TA_CENTER)),
                Paragraph("шт", ParagraphStyle('Center2', parent=styles['CellNormal'], alignment=TA_CENTER)),
                Paragraph(f"{item['price_rub']:,.2f}", ParagraphStyle('Right', parent=styles['CellNormal'], alignment=TA_RIGHT)),
                Paragraph(f"{item['price_rub']:,.2f}", ParagraphStyle('Right2', parent=styles['CellNormal'], alignment=TA_RIGHT)),
            ])
        
        total = order['total_rub']
        vat_rate = buyer.get('vat_rate', 20)
        vat_amount = total * vat_rate / 120 if vat_rate > 0 else 0
        
        table_data.append([
            "", "", "", "",
            Paragraph("<b>Итого:</b>", ParagraphStyle('Total', parent=styles['CellBold'], alignment=TA_RIGHT)),
            Paragraph(f"<b>{total:,.2f}</b>", ParagraphStyle('Total2', parent=styles['CellBold'], alignment=TA_RIGHT)),
        ])
        
        if vat_rate > 0:
            table_data.append([
                "", "", "", "",
                Paragraph(f"В т.ч. НДС {vat_rate}%:", ParagraphStyle('VAT', parent=styles['CellNormal'], alignment=TA_RIGHT)),
                Paragraph(f"{vat_amount:.2f}", ParagraphStyle('VAT2', parent=styles['CellNormal'], alignment=TA_RIGHT)),
            ])
        
        table_data.append([
            "", "", "", "",
            Paragraph("<b>Всего к оплате:</b>", ParagraphStyle('GrandTotal', parent=styles['CellBold'], alignment=TA_RIGHT)),
            Paragraph(f"<b>{total:,.2f}</b>", ParagraphStyle('GrandTotal2', parent=styles['CellBold'], alignment=TA_RIGHT)),
        ])
        
        items_table = Table(table_data, colWidths=[15*mm, 85*mm, 20*mm, 20*mm, 25*mm, 25*mm])
        items_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f5f5f5')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('FONTNAME', (0, 0), (-1, 0), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -3), 0.5, colors.black),
            ('LINEBELOW', (0, -3), (-1, -1), 0.5, colors.black),
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('ALIGN', (2, 0), (3, -1), 'CENTER'),
            ('ALIGN', (4, 0), (-1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
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
        
        elements.append(Paragraph(total_text, ParagraphStyle('TotalWords', parent=styles['Normal'], fontSize=10, spaceAfter=10)))
        elements.append(Spacer(1, 10*mm))
        
        # ==================== БАНКОВСКИЕ РЕКВИЗИТЫ С QR-КОДОМ ====================
        elements.append(Paragraph("<b>Банковские реквизиты:</b>", ParagraphStyle('BankTitle', parent=styles['Normal'], fontSize=10, fontName=self.font_bold, spaceAfter=8)))
        
        bank_info = []
        if seller.get('bank_name'):
            bank_info.append([Paragraph(f"Банк: {seller['bank_name']}", styles['CellNormal'])])
        if seller.get('bank_account'):
            bank_info.append([Paragraph(f"Расчётный счёт: {seller['bank_account']}", styles['CellNormal'])])
        if seller.get('bank_bik'):
            bank_info.append([Paragraph(f"БИК: {seller['bank_bik']}", styles['CellNormal'])])
        if seller.get('bank_corr'):
            bank_info.append([Paragraph(f"Корр. счёт: {seller['bank_corr']}", styles['CellNormal'])])
        
        try:
            qr_buffer = self._create_qr_code(seller, total)
            qr_img = Image(qr_buffer, width=40*mm, height=40*mm)
            bank_table = Table([[bank_info, qr_img]], colWidths=[130*mm, 50*mm])
            bank_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ]))
            elements.append(bank_table)
        except Exception as e:
            logger.error(f"Ошибка создания QR-кода: {e}")
            bank_table = Table(bank_info, colWidths=[170*mm])
            bank_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, -1), self.font_normal),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f9f9f9')),
                ('PADDING', (0, 0), (-1, -1), 5),
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
            ParagraphStyle('OfferFooter', parent=styles['Normal'], fontSize=8, textColor=colors.grey, alignment=TA_LEFT, leading=10)
        ))
        
        # ==================== ГЕНЕРАЦИЯ ====================
        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        logger.info(f"✅ PDF сгенерирован: {invoice_number} ({len(pdf_bytes)} байт)")
        return pdf_bytes