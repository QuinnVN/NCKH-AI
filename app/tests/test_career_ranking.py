import json
import re
from pathlib import Path
import unittest
from app.career_ranking import CATALOG, rank_careers, career_description
from app.final_evaluation_calculation import DimensionFact, BehaviourFact


class CareerRankingTests(unittest.TestCase):
    def test_catalog_covers_every_occupation_with_distinct_complete_profiles(self):
        self.assertEqual(len(CATALOG['careers']), 68)
        mapping = json.loads((Path(__file__).resolve().parents[2] / 'data/onet-career-mapping.json').read_text(encoding='utf-8'))
        self.assertEqual({career['id'] for career in CATALOG['careers']}, {career['id'] for career in mapping['careers']})
        profiles = set()
        for career in CATALOG['careers']:
            self.assertEqual(set(career['weights']), set(CATALOG['dimensionNames']))
            self.assertEqual(set(career['profile']), {code for code in CATALOG['dimensionNames'] if not code.startswith('D')})
            self.assertTrue(career['onetSoc'])
            self.assertTrue(all(1 <= value <= 5 for value in career['weights'].values()))
            profile = json.dumps([career['weights'], career['desireTargets']], sort_keys=True)
            self.assertNotIn(profile, profiles)
            profiles.add(profile)

    def test_shared_web_engine_fixtures(self):
        fixtures = json.loads((Path(__file__).resolve().parents[2] / 'data/career-ranking-fixtures.json').read_text(encoding='utf-8'))
        for fixture in fixtures:
            dimensions = {code: DimensionFact(code, score, CATALOG['dimensionNames'][code], '') for code, score in fixture['scores'].items()}
            observed = {item['code']: BehaviourFact(item['code'], item['label'], item['score'], item['evidence'], 50) for item in fixture['behaviours']}
            ranked = rank_careers(dimensions, observed, fixture['interests'])
            self.assertEqual([item.career['id'] for item in ranked], [item['id'] for item in fixture['expected']])
            for item, expected in zip(ranked, fixture['expected'], strict=True):
                self.assertAlmostEqual(item.score, expected['score'], places=10)
                self.assertEqual(item.compatibility_percent, expected['percentage'])
                self.assertIn(item.career['activity'], career_description(item))
                for primary in (True, False):
                    description = career_description(item, primary=primary)
                    count = len(re.split(r'(?<=[.!?])\s+', description))
                    self.assertIn(count, (3, 4) if primary else (2, 3))
                    for fact in item.dimensions[:2]:
                        self.assertIn(f"{fact['score']:g}/100", description)
                    if item.observations:
                        self.assertIn(item.observations[0]['evidence'], description)
                    self.assertIn(item.career['activity'], description)


if __name__ == '__main__':
    unittest.main()
