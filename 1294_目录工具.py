"""Offline CSV to PDF catalog generator. CLI: python 1294_目录工具.py --help."""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph

PREFIX = '1294'
REQUIRED = ('sku', 'name', 'category', 'description', 'price', 'currency', 'image')
PAPER = (595.276, 841.89)
INK = colors.HexColor('#173C3A')
MUTED = colors.HexColor('#576D6B')
ACCENT = colors.HexColor('#D96E50')
PALE = colors.HexColor('#F4F3EE')
pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))


class CatalogError(ValueError):
    """An actionable input or output error."""


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    description: str
    price: Decimal
    currency: str
    image: Path | None


def load_products(source: Path) -> list[Product]:
    """Validate the complete input before creating any output."""
    try:
        source = Path(source).resolve()
    except (OSError, ValueError) as exc:
        raise CatalogError(f'Invalid CSV path: {exc}') from exc
    if not source.is_file():
        raise CatalogError(f'CSV not found: {source}')
    if source.stat().st_size > 20 * 1024 * 1024:
        raise CatalogError('CSV exceeds 20 MB. Split the input into smaller batches.')
    products, seen, issues = [], set(), []
    try:
        with source.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, strict=True)
            fields = reader.fieldnames or []
            if len(set(fields)) != len(fields):
                raise CatalogError('CSV contains duplicate column names.')
            missing = set(REQUIRED) - set(fields)
            if missing:
                raise CatalogError('Missing columns: ' + ', '.join(sorted(missing)))
            for row in reader:
                line = reader.line_num
                if len(products) >= 10000:
                    raise CatalogError('Limit is 10,000 products per catalog.')
                if None in row or any(row[k] is None for k in REQUIRED):
                    issues.append(f'Line {line}: column count does not match the header.')
                    continue
                row = {k: (row[k] or '').strip() for k in REQUIRED}
                before = len(issues)
                for field, limit in [('sku', 64), ('name', 160), ('category', 70), ('description', 700)]:
                    if not row[field] or len(row[field]) > limit:
                        issues.append(f'Line {line}: {field} is required and must be at most {limit} characters.')
                    if any(ord(ch) < 32 and ch not in '\n\r\t' for ch in row[field]):
                        issues.append(f'Line {line}: {field} contains unsupported control characters.')
                key = row['sku'].casefold()
                if key in seen:
                    issues.append(f'Line {line}: duplicate SKU {row["sku"]!r}.')
                seen.add(key)
                try:
                    price = Decimal(row['price'])
                    if not price.is_finite() or price < 0 or price > Decimal('999999999.99'):
                        raise InvalidOperation
                    if price.as_tuple().exponent < -2 or price != price.quantize(Decimal('.01')):
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    price = Decimal(0)
                    issues.append(f'Line {line}: price must be 0..999999999.99 with at most two decimal places.')
                currency = row['currency'].upper()
                if not re.fullmatch(r'[A-Z]{3}', currency):
                    issues.append(f'Line {line}: currency must be a three-letter code, e.g. USD.')
                image = None
                if row['image']:
                    candidate = Path(row['image'])
                    try:
                        image = (source.parent / candidate).resolve()
                    except (OSError, ValueError) as exc:
                        issues.append(f'Line {line}: invalid image path: {exc}')
                        continue
                    if candidate.is_absolute() or not image.is_relative_to(source.parent):
                        issues.append(f'Line {line}: image must be a local relative path inside the CSV folder.')
                    else:
                        try:
                            with Image.open(image) as picture:
                                if picture.width * picture.height > 30_000_000:
                                    raise ValueError('image exceeds 30 megapixels')
                                if picture.format not in ('PNG', 'JPEG', 'WEBP'):
                                    raise ValueError('use PNG, JPEG or WEBP')
                                picture.verify()
                        except (OSError, ValueError, Image.DecompressionBombError) as exc:
                            issues.append(f'Line {line}: unreadable image {row["image"]!r}: {exc}')
                if len(issues) == before:
                    products.append(Product(row['sku'], row['name'], row['category'], row['description'], price, currency, image))
                if len(issues) >= 30:
                    break
    except (UnicodeError, csv.Error) as exc:
        raise CatalogError(f'Invalid CSV. Save as UTF-8 CSV: {exc}') from exc
    if issues:
        raise CatalogError('\n'.join(issues[:30]))
    if not products:
        raise CatalogError('CSV contains no product rows.')
    return products


def font_for(text: str, bold: bool = False) -> str:
    if any(ord(ch) > 255 for ch in text):
        return 'STSong-Light'
    return 'Helvetica-Bold' if bold else 'Helvetica'


def paragraph(pdf, text, x, y, width, size=11, color=INK, bold=False, max_height=None):
    """Draw wrapped text from a top coordinate, preserving XML-like product names."""
    style = ParagraphStyle('text', fontName=font_for(text, bold), fontSize=size,
                           leading=size * 1.35, textColor=color, alignment=TA_LEFT,
                           splitLongWords=True, wordWrap='CJK' if font_for(text) == 'STSong-Light' else None)
    item = Paragraph(escape(text).replace('\n', '<br/>'), style)
    _, height = item.wrap(width, PAPER[1])
    if max_height is not None and height > max_height:
        raise CatalogError('Text does not fit the catalog layout. Shorten the product name or description.')
    item.drawOn(pdf, x, y - height)
    return height


def make_catalog(source: Path, destination: Path, title='Everyday essentials') -> dict:
    products = load_products(Path(source))
    if not title.strip() or len(title) > 90:
        raise CatalogError('Catalog title must contain 1..90 characters.')
    try:
        destination = Path(destination).resolve()
    except (OSError, ValueError) as exc:
        raise CatalogError(f'Invalid output path: {exc}') from exc
    if destination.suffix.lower() != '.pdf':
        raise CatalogError('Output filename must end in .pdf.')
    if destination.exists():
        raise CatalogError(f'Output already exists. Choose another filename: {destination.name}')
    if not destination.parent.is_dir():
        raise CatalogError('Output folder does not exist.')
    groups = OrderedDict()
    for product in products:
        groups.setdefault(product.category, []).append(product)
    pages = [(category, items[i:i + 4]) for category, items in groups.items() for i in range(0, len(items), 4)]
    handle, temporary = tempfile.mkstemp(prefix='.1294-', suffix='.pdf', dir=destination.parent)
    os.close(handle)
    try:
        pdf = canvas.Canvas(temporary, pagesize=PAPER, pageCompression=1, invariant=1)
        pdf.setTitle(title)
        pdf.setAuthor('Independent Python automation portfolio project')
        for number, (category, items) in enumerate(pages, 1):
            w, h = PAPER
            pdf.setFillColor(INK)
            pdf.rect(0, h - 12, w, 12, fill=1, stroke=0)
            paragraph(pdf, 'COLLECTION / PRODUCT CATALOG', 38, h - 40, 440, 8, MUTED, True)
            paragraph(pdf, title, 38, h - 64, w - 76, 25, INK, True, 70)
            paragraph(pdf, category, 38, h - 140, w - 140, 14, INK, True, 40)
            pdf.setFillColor(ACCENT)
            pdf.circle(w - 43, h - 146, 5, fill=1, stroke=0)
            for position, product in enumerate(items):
                x = 38 + (position % 2) * 266
                y = h - 198 - (position // 2) * 284
                width, image_height = 253, 122
                pdf.setFillColor(PALE)
                pdf.roundRect(x, y - image_height, width, image_height, 7, fill=1, stroke=0)
                if product.image:
                    with Image.open(product.image) as original:
                        picture = ImageOps.exif_transpose(original).convert('RGB')
                        picture.thumbnail((1000, 600))
                        # In-memory decoding bounds embedded images and closes file handles.
                        image_data = io.BytesIO()
                        picture.save(image_data, format='JPEG', quality=90)
                        image_data.seek(0)
                        pdf.drawImage(ImageReader(image_data), x + 8, y - image_height + 8,
                                      width - 16, image_height - 16, preserveAspectRatio=True, anchor='c')
                else:
                    paragraph(pdf, 'IMAGE OPTIONAL', x + 58, y - 54, 145, 9, MUTED)
                cursor = y - image_height - 13
                cursor -= paragraph(pdf, product.name, x, cursor, width, 13, INK, True, 58) + 7
                cursor -= paragraph(pdf, product.description, x, cursor, width, 9.5, MUTED, False, 69) + 9
                if cursor < y - 235:
                    raise CatalogError(f'Text for SKU {product.sku} is too long for a card. Shorten its name or description.')
                paragraph(pdf, f'{product.currency} {product.price:,.2f}', x, y - 241, width, 12, INK, True)
                paragraph(pdf, product.sku, x, y - 268, width, 8, MUTED)
            pdf.setStrokeColor(colors.HexColor('#D8DFDA'))
            pdf.line(38, 42, w - 38, 42)
            paragraph(pdf, f'{len(products)} products / {len(groups)} categories', 38, 31, 350, 8, MUTED)
            pdf.setFont('Helvetica', 8)
            pdf.setFillColor(MUTED)
            pdf.drawRightString(w - 38, 20, f'{number:02d} / {len(pages):02d}')
            pdf.showPage()
        pdf.save()
        # Commit only a finished PDF. Both operations fail if the destination exists.
        # The temporary file is on the same filesystem as the destination.
        if os.name == 'nt':
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        return {'products': len(products), 'categories': len(groups), 'pages': len(pages), 'output': str(destination)}
    except FileExistsError as exc:
        raise CatalogError('Another file already uses the selected output name.') from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title('CSV to PDF Catalog | 离线商品目录工具')
    root.geometry('720x480')
    root.minsize(720, 480)
    root.configure(bg='#F4F3EE')
    frame = ttk.Frame(root, padding=28)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='CSV → PDF 商品目录', font=('Microsoft YaHei UI', 22, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='离线生成 · 数据校验 · 分类分页 · 保留原文件', font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(6, 22))
    source = tk.StringVar()
    title = tk.StringVar(value='Everyday essentials')
    ttk.Label(frame, text='商品 CSV 文件（UTF-8）').pack(anchor='w')
    row = ttk.Frame(frame)
    row.pack(fill='x', pady=(5, 15))
    ttk.Entry(row, textvariable=source).pack(side='left', fill='x', expand=True)
    def choose():
        filename = filedialog.askopenfilename(filetypes=[('CSV 文件', '*.csv')])
        if filename:
            source.set(filename)
    ttk.Button(row, text='选择文件', command=choose).pack(side='right', padx=(10, 0))
    ttk.Label(frame, text='目录标题').pack(anchor='w')
    ttk.Entry(frame, textvariable=title).pack(fill='x', pady=(5, 20))
    status = tk.StringVar(value='准备就绪。示例数据与程序放在同一文件夹。')
    def run():
        if not source.get():
            messagebox.showerror('请选择输入', '请先选择商品 CSV 文件。')
            return
        output = filedialog.asksaveasfilename(defaultextension='.pdf', initialfile='1294_商品目录.pdf', filetypes=[('PDF 文件', '*.pdf')])
        if not output:
            return
        try:
            info = make_catalog(Path(source.get()), Path(output), title.get())
            status.set(f'已生成：{info["products"]} 件商品，{info["pages"]} 页。')
            messagebox.showinfo('生成成功', f'{status.get()}\n{output}')
        except (CatalogError, OSError) as exc:
            status.set('未生成，请检查输入。')
            messagebox.showerror('输入或输出错误', str(exc))
    ttk.Button(frame, text='生成 PDF 目录', command=run).pack(anchor='w', pady=5)
    ttk.Label(frame, textvariable=status, wraplength=640).pack(anchor='w', pady=(20, 12))
    ttk.Label(frame, text='独立开发案例。示例商品与图片均为自制数据。', foreground='#576D6B').pack(anchor='w')
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description='Generate an offline product catalog from a UTF-8 CSV.')
    parser.add_argument('csv', nargs='?', type=Path)
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--title', default='Everyday essentials')
    parser.add_argument('--check', action='store_true', help='Validate input without creating a PDF')
    args = parser.parse_args()
    if args.csv is None:
        launch_gui()
        return 0
    try:
        if args.check:
            print(f'Valid: {len(load_products(args.csv))} products')
        else:
            if not args.output:
                parser.error('--output is required unless --check is used')
            info = make_catalog(args.csv, args.output, args.title)
            print(f'Created {info["pages"]} pages / {info["products"]} products: {info["output"]}')
        return 0
    except (CatalogError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
