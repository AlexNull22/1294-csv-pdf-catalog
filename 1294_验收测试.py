"""Behavior acceptance tests. Generated files use the OS temporary directory."""
from __future__ import annotations
import contextlib
import csv
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from decimal import Decimal
from pypdf import PdfReader
from reportlab.pdfgen import canvas

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
PROJECT = HERE
TEST_WORK = Path(tempfile.gettempdir())
SPEC = importlib.util.spec_from_file_location('catalog_under_test', PROJECT / '1294_目录工具.py')
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)

class CatalogAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='1294_验收_', dir=TEST_WORK)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def row(self, **changes):
        result = dict(sku='QA-001', name='Modular Desk Organizer', category='Workspace',
                      description='A compact original example product for a clean desktop.',
                      price='23.45', currency='USD', image='')
        result.update(changes)
        return result

    def write_csv(self, rows, columns=None, name='1294_测试输入.csv', encoding='utf-8-sig'):
        path = self.folder / name
        fields = list(columns if columns is not None else app.REQUIRED)
        with path.open('w',encoding=encoding,newline='') as f:
            writer=csv.writer(f)
            writer.writerow(fields)
            for row in rows:
                writer.writerow([row.get(field,'') for field in fields] if isinstance(row,dict) else row)
        return path

    def text(self,path):
        reader=PdfReader(path)
        return reader, '\n'.join(page.extract_text() or '' for page in reader.pages)

    def digest(self,path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_sample_pages_all_skus_and_amounts(self):
        source=PROJECT/'1294_示例商品.csv'
        products=app.load_products(source)
        before=self.digest(source)
        destination=self.folder/'1294_商品目录.pdf'
        result=app.make_catalog(source,destination)
        reader,text=self.text(destination)
        self.assertEqual((result['products'],result['categories'],result['pages']),(8,2,2))
        self.assertEqual(len(reader.pages),2)
        for product in products:
            self.assertEqual(text.count(product.sku),1,product.sku)
            self.assertIn(f'{product.currency} {product.price:,.2f}',text)
        self.assertEqual(self.digest(source),before)
        self.assertEqual(destination.read_bytes()[:5],b'%PDF-')

    def test_utf8_chinese_with_and_without_bom(self):
        for encoding in ('utf-8','utf-8-sig'):
            with self.subTest(encoding=encoding):
                source=self.write_csv([self.row(name='弧线桌灯',description='中文说明：用于检查中文商品名称和换行。',category='桌面用品')],name=f'1294_{encoding}.csv',encoding=encoding)
                destination=self.folder/f'1294_{encoding}.pdf'
                result=app.make_catalog(source,destination,title='中文商品目录')
                _,text=self.text(destination)
                self.assertEqual(result['products'],1)
                for term in ('中文商品目录','弧线桌灯','桌面用品','中文说明'):
                    self.assertIn(term,text)

    def test_missing_columns(self):
        source=self.write_csv([self.row()],columns=[c for c in app.REQUIRED if c!='price'])
        with self.assertRaisesRegex(app.CatalogError,'Missing columns.*price'):
            app.load_products(source)

    def test_duplicate_columns(self):
        source=self.write_csv([self.row()],columns=[*app.REQUIRED,'sku'])
        with self.assertRaisesRegex(app.CatalogError,'duplicate column'):
            app.load_products(source)

    def test_duplicate_sku_case_insensitive(self):
        source=self.write_csv([self.row(sku='Qa-001'),self.row(sku='qA-001')])
        with self.assertRaisesRegex(app.CatalogError,'duplicate SKU'):
            app.load_products(source)

    def test_invalid_prices(self):
        for value in ('NaN','sNaN','Infinity','-Infinity','-0.01','1.234','1000000000.00','not-a-price',''):
            with self.subTest(price=value):
                source=self.write_csv([self.row(price=value)])
                with self.assertRaisesRegex(app.CatalogError,'price must'):
                    app.load_products(source)

    def test_three_decimal_places_including_trailing_zero(self):
        source=self.write_csv([self.row(price='1.230')])
        with self.assertRaisesRegex(app.CatalogError,'price must'):
            app.load_products(source)

    def test_valid_prices_zero_max_and_two_decimals(self):
        for value in ('0','0.00','23.45','999999999.99'):
            with self.subTest(price=value):
                source=self.write_csv([self.row(price=value)])
                self.assertEqual(app.load_products(source)[0].price,Decimal(value))

    def test_missing_image(self):
        source=self.write_csv([self.row(image='1294_不存在.png')])
        with self.assertRaisesRegex(app.CatalogError,'unreadable image'):
            app.load_products(source)

    def test_image_path_traversal_and_absolute_path(self):
        for value in ('../1294_外部图片.png',str(PROJECT/'1294_商品图片01.png')):
            with self.subTest(path=value):
                source=self.write_csv([self.row(image=value)])
                with self.assertRaisesRegex(app.CatalogError,'local relative path'):
                    app.load_products(source)

    def test_invalid_image_null_path_is_actionable(self):
        source=self.write_csv([self.row(image='bad\x00name.png')])
        with self.assertRaises(app.CatalogError):
            app.load_products(source)

    def test_no_overwrite(self):
        source=self.write_csv([self.row()])
        destination=self.folder/'1294_已存在.pdf'
        original=b'original existing file, must survive unchanged'
        destination.write_bytes(original)
        before=self.digest(source)
        with self.assertRaisesRegex(app.CatalogError,'already exists'):
            app.make_catalog(source,destination)
        self.assertEqual(destination.read_bytes(),original)
        self.assertEqual(self.digest(source),before)

    def test_501_and_1001_rows_paginate_and_preserve_every_sku(self):
        for count in (501,1001):
            with self.subTest(rows=count):
                rows=[self.row(sku=f'SCALE-{i:04d}') for i in range(count)]
                source=self.write_csv(rows,name=f'1294_规模{count}.csv')
                destination=self.folder/f'1294_规模{count}.pdf'
                result=app.make_catalog(source,destination,title='Scale acceptance catalog')
                reader,text=self.text(destination)
                self.assertEqual(result['pages'],(count+3)//4)
                self.assertEqual(len(reader.pages),(count+3)//4)
                for row in rows:
                    self.assertEqual(text.count(row['sku']),1,row['sku'])
                self.assertEqual(text.count('USD 23.45'),count)

    def test_grouping_preserves_all_rows_and_stable_category_order(self):
        rows=[self.row(sku=f'GROUP-{i}',category=('Beta' if i%2==0 else 'Alpha')) for i in range(10)]
        source=self.write_csv(rows)
        result=app.make_catalog(source,self.folder/'1294_分类.pdf')
        reader,text=self.text(self.folder/'1294_分类.pdf')
        self.assertEqual((result['pages'],result['categories']),(4,2))
        self.assertIn('Beta',reader.pages[0].extract_text())
        self.assertIn('Alpha',reader.pages[2].extract_text())
        for row in rows:self.assertEqual(text.count(row['sku']),1)

    def test_empty_data_header_only_and_empty_file(self):
        for content in (','.join(app.REQUIRED)+'\n',''):
            with self.subTest(content=bool(content)):
                source=self.folder/'1294_空数据.csv';source.write_text(content,encoding='utf-8-sig')
                with self.assertRaises(app.CatalogError):app.load_products(source)

    def test_short_and_extra_csv_rows(self):
        for row in (['SKU','Name'],list(self.row().values())+['unmapped extra value']):
            with self.subTest(row=row):
                source=self.write_csv([row])
                with self.assertRaisesRegex(app.CatalogError,'column count'):
                    app.load_products(source)

    def test_embedded_newline_quotes_and_xml_escaped_text(self):
        source=self.write_csv([self.row(name='Desk <A> & "B"',description='First line, quoted "detail".\nSecond line.')])
        app.make_catalog(source,self.folder/'1294_引号换行.pdf')
        _,text=self.text(self.folder/'1294_引号换行.pdf')
        self.assertIn('Desk <A> & "B"',text)
        self.assertIn('First line, quoted "detail".',text)
        self.assertIn('Second line.',text)

    def test_layout_valid_long_content_never_overlaps_price_and_sku(self):
        scratch=canvas.Canvas(io.BytesIO())
        def pick(word,limit,size,maxheight):
            best=''
            for n in range(1,200):
                candidate=(' '.join([word]*n))[:limit]
                try:
                    height=app.paragraph(scratch,candidate,0,800,253,size,max_height=maxheight)
                except app.CatalogError:break
                best=candidate
                if len(candidate)==limit:break
            return best
        name=pick('Modular',160,13,58)
        description=pick('storage',700,9.5,69)
        source=self.write_csv([self.row(name=name,description=description)])
        draws=[]
        original=app.paragraph
        def record(pdf,text,x,y,width,size=11,color=app.INK,bold=False,max_height=None):
            height=original(pdf,text,x,y,width,size,color,bold,max_height)
            draws.append(dict(text=text,x=x,y=y,height=height))
            return height
        destination=self.folder/'1294_长文本.pdf'
        try:
            with mock.patch.object(app,'paragraph',side_effect=record):
                app.make_catalog(source,destination)
        except app.CatalogError as exc:
            self.assertRegex(str(exc),'[Tt]ext|[Ss]horten|fit')
            self.assertFalse(destination.exists())
            self.assertFalse(list(self.folder.glob('.1294-*.pdf')))
            return
        price=next(v for v in draws if v['text']=='USD 23.45')
        sku=next(v for v in draws if v['text']=='QA-001')
        self.assertGreaterEqual(price['y']-price['height'],sku['y']+3,
            f'Price span [{price["y"]-price["height"]:.2f},{price["y"]:.2f}] overlaps SKU span [{sku["y"]-sku["height"]:.2f},{sku["y"]:.2f}]. Name chars={len(name)}, description chars={len(description)}')

    def test_missing_output_folder_leaves_original_unchanged(self):
        source=self.write_csv([self.row()]);before=self.digest(source)
        destination=self.folder/'1294_不存在目录'/'1294_输出.pdf'
        with self.assertRaisesRegex(app.CatalogError,'folder does not exist'):
            app.make_catalog(source,destination)
        self.assertEqual(self.digest(source),before)
        self.assertFalse(destination.exists())

    def test_output_save_failure_preserves_input_and_removes_partial_destination(self):
        source=self.write_csv([self.row()]);before=self.digest(source)
        destination=self.folder/'1294_写入失败.pdf'
        with mock.patch.object(app.canvas.Canvas,'save',side_effect=OSError('Simulated disk full')):
            with self.assertRaisesRegex(OSError,'Simulated disk full'):
                app.make_catalog(source,destination)
        self.assertEqual(self.digest(source),before)
        self.assertFalse(list(self.folder.glob('.1294-*.pdf')),'Temporary PDF must be cleaned')
        self.assertFalse(destination.exists(),'Failed export must not leave a partial final PDF')

    def test_output_commit_failure_preserves_input_and_removes_partial_destination(self):
        source=self.write_csv([self.row()]);before=self.digest(source)
        destination=self.folder/'1294_提交失败.pdf'
        operation='rename' if app.os.name=='nt' else 'link'
        with mock.patch.object(app.os,operation,side_effect=OSError('Simulated commit failure')):
            with self.assertRaisesRegex(OSError,'Simulated commit failure'):
                app.make_catalog(source,destination)
        self.assertEqual(self.digest(source),before)
        self.assertFalse(list(self.folder.glob('.1294-*.pdf')),'Temporary PDF must be cleaned')
        self.assertFalse(destination.exists(),'Failed commit must not leave a partial final PDF')

    def test_gui_routing_without_cli_args(self):
        with mock.patch.object(sys,'argv',['1294_目录工具.py']), mock.patch.object(app,'launch_gui') as launch:
            self.assertEqual(app.main(),0)
            launch.assert_called_once_with()

    def test_cli_check_has_no_output_side_effect(self):
        source=self.write_csv([self.row()]);before=set(self.folder.iterdir())
        with mock.patch.object(sys,'argv',['1294_目录工具.py',str(source),'--check']), contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(app.main(),0)
        self.assertIn('Valid: 1 products',stream.getvalue())
        self.assertEqual(set(self.folder.iterdir()),before)

if __name__=='__main__':
    report=io.StringIO()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(CatalogAcceptance)
    result=unittest.TextTestRunner(stream=report,verbosity=2).run(suite)
    summary=report.getvalue()
    (TEST_WORK/'1294_验收结果.txt').write_text(summary,encoding='utf-8')
    print(summary)
    raise SystemExit(not result.wasSuccessful())
