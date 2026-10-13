import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import description_translation as translation
import test_maintenance as fixtures


class DescriptionTranslationTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill

    def test_translates_once_and_uses_persistent_cache(self):
        text = 'Recommend and price-check AI models for any task.'
        with patch.object(translation, 'model_available', return_value=True), patch.object(
            translation, 'translate', return_value='推荐并比价适合各种任务的 AI 模型。'
        ) as model:
            first = translation.descriptions(self.m, {'items': [text, text]})
            second = translation.descriptions(self.m, {'items': [text]})
        self.assertEqual(first['translations'][text], '推荐并比价适合各种任务的 AI 模型。')
        self.assertEqual(second, first)
        model.assert_called_once_with(text)

    def test_unavailable_model_does_not_invent_translation(self):
        with patch.object(translation, 'model_available', return_value=False):
            result = translation.descriptions(self.m, {'items': ['Discover AI skills']})
        self.assertEqual(result, {'translations': {}})

    def test_rejects_unbounded_requests(self):
        with self.assertRaises(ValueError):
            translation.descriptions(self.m, {'items': ['a'] * 13})


if __name__ == '__main__':
    unittest.main()
