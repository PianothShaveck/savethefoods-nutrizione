import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
import savethefoods as stf


class NutritionTests(unittest.TestCase):
    def parse(self, markup):
        return stf.parse_nutrition(BeautifulSoup(markup, 'html.parser'))

    def test_current_table_and_missing_fibre(self):
        p=self.parse('''<table class="envision-nutrition__table"><thead><tr><th>Nutrienti</th><th>Per 100 g</th></tr></thead><tbody><tr><td>Energia</td><td>92 kJ / 23 kcal</td></tr><tr><td>Grassi</td><td>0.10 g</td></tr><tr><td>di cui saturi</td><td>0.02 g</td></tr><tr><td>Proteine</td><td>1.10 g</td></tr></tbody></table>''')
        self.assertEqual((p['calorie_kcal'],p['grassi_saturi_g'],p['proteine_g']),(23,.02,1.1))
        self.assertIsNone(p['fibre_g'])
        self.assertEqual(p['base_nutrizionale'],'100 g')

    def test_legacy_prose_bounds_and_100ml(self):
        p=self.parse('''<div id="tab-description"><div><strong>Valori nutrizionali (per 100 ml)</strong>: – Energia: 245 kJ / 58 kcal – Grassi: 1,2 g – di cui saturi: 0,2 g – Carboidrati: 11 g – di cui zuccheri: 4,1 g – Proteine: 0,8 g – Fibre: &lt;0,5 g – Sale: 0,1 g</div></div>''')
        stf.metrics(p)
        self.assertEqual(p['grassi_saturi_g'],.2)
        self.assertEqual((p['fibre_g'],p['fibre_g_limite']),(.5,'<'))
        self.assertEqual(p['base_nutrizionale'],'100 ml')
        self.assertIsNone(p['fibre_100kcal'])
        self.assertAlmostEqual(p['proteine_100kcal'],.8/58*100,places=4)

    def test_multi_column_table(self):
        p=self.parse('<table class="envision-nutrition__table"><thead><tr><th>Nutriente</th><th>Per porzione</th><th>Per 100 g</th></tr></thead><tr><td>Proteine</td><td>3 g</td><td>10 g</td></tr></table>')
        self.assertEqual(p['proteine_g'],10)

    def test_no_invented_basis_or_zero(self):
        p=self.parse('<div id="tab-description">Valori nutrizionali: Energia: 200 kcal; Proteine: 10 g; Grassi: 0 g</div>')
        stf.metrics(p)
        self.assertEqual(p['grassi_g'],0)
        self.assertIsNone(p['fibre_g'])
        self.assertEqual(p['base_nutrizionale'],'')
        self.assertIsNone(p['proteine_100kcal'])

    def test_malformed_number_not_concatenated(self):
        p=self.parse('<div id="tab-description">Valori nutrizionali (per 100 g): Energia: 435 kcal – Fibre: 3,31,2 g – Proteine: 4,9 g</div>')
        self.assertIsNone(p['fibre_g'])
        self.assertTrue(p['note'])

    def test_inconsistent_site_data_not_ranked(self):
        p=self.parse('<div id="tab-description">Valori nutrizionali (per 100 g): Energia: 1222 kJ / 21 kcal – Grassi: 1 g – di cui saturi: 2 g – Proteine: 2 g</div>')
        stf.metrics(p)
        self.assertEqual(p['calorie_kcal'],21)
        self.assertIsNone(p['proteine_100kcal'])
        self.assertTrue(any('kJ' in n for n in p['note']))

    def test_product_scope_ignores_related(self):
        markup='''<div class="type-product"><h1 class="product_title">Test</h1><p class="price"><del><span class="amount">3,00 €</span></del><ins><span class="amount">1,50 €</span></ins></p><p class="stock in-stock">2 disponibili</p><div id="tab-description">Valori nutrizionali (per 100 g): Energia: 100 kcal – Proteine: 1 g</div><section class="related"><p class="stock out-of-stock">Esaurito</p><table class="envision-nutrition__table"><tr><td>Proteine</td><td>90 g</td></tr></table></section></div>'''
        p=stf.record_from_page({'permalink':stf.BASE+'/prodotto/test/'},markup)
        self.assertTrue(p['disponibile'])
        self.assertEqual(p['prezzo_eur'],1.5)
        self.assertEqual(p['proteine_g'],1)

    def test_empty_new_tab_does_not_hide_legacy(self):
        p=self.parse('<div class="woocommerce-Tabs-panel" id="tab-custom"></div><div id="tab-description">Valori nutrizionali per 100 g: Energia: 300 kcal; Proteine: 10 g</div>')
        self.assertEqual(p['calorie_kcal'],300)

    def test_failed_scrape_preserves_saved_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            cache=Path(temp)/'prodotti_savethefoods.json';cache.write_text('{"previous":true}')
            with patch('sys.argv',['savethefoods.py','--no-browser','--cartella',temp]),patch.object(stf,'catalog_api',return_value=[{'permalink':stf.BASE+'/prodotto/test/'}]),patch.object(stf,'scrape',side_effect=ValueError('Pagina cambiata')):
                self.assertEqual(stf.main(),1)
            self.assertEqual(json.loads(cache.read_text()),{'previous':True})

    def test_embedded_json_is_not_executable_html(self):
        page=stf.generate_html({'products':[{'nome':'</script><script>alert(1)</script>'}]})
        self.assertNotIn('</script><script>alert(1)',page)
        self.assertIn('\\u003c/script',page)


if __name__=='__main__':
    unittest.main()
