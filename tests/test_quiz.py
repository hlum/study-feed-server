"""POST /cards/{id}/quiz, with Claude and the page fetch stubbed out (no network, no Pro usage).

  .venv/bin/python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.TemporaryDirectory(prefix="studyfeed-test-")
os.environ["DB_PATH"] = str(Path(TMP.name) / "test.db")  # before core reads it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import app  # noqa: E402
from core import connect, init_db  # noqa: E402

JA_QUIZ = {"question": "SwiftUI の @State はどこに値を保存しますか？",
           "options": ["ビューの外の SwiftUI 管理ストレージ", "ビュー構造体のプロパティそのもの",
                       "UserDefaults", "Keychain"],
           "answer": 0, "explanation": "@State の値はビューの外で SwiftUI が管理し、再描画をまたいで保持されます。"}
EN_QUIZ = {"question": "Where does SwiftUI keep @State values?",
           "options": ["Storage managed by SwiftUI", "The struct itself", "UserDefaults", "Keychain"],
           "answer": 0, "explanation": "SwiftUI stores them outside the view so they survive redraws."}


def article(lang: str, quiz=None) -> dict:
    return {"card_type": "article", "topic": "ios", "series_id": None, "part": None,
            "parts_total": None, "title": "SwiftUI の @State 入門" if lang == "ja" else "SwiftUI @State",
            "body": "@State の仕組みを解説します。" if lang == "ja" else "How @State works.",
            "source": {"label": "Zenn", "url": "https://zenn.dev/x/articles/1", "quote": None},
            "slides": [], "visual": None, "cliffhanger": None, "quiz": quiz, "flashcard": None,
            "kind": "news", "lang": lang, "site": "Zenn", "published_at": None}


class QuizTest(unittest.TestCase):
    def setUp(self):
        Path(os.environ["DB_PATH"]).unlink(missing_ok=True)
        init_db()
        # No page fetch: the quiz falls back to the card body.
        patcher = mock.patch.object(app, "reader_view", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, card: dict) -> int:
        con = connect()
        cur = con.execute("INSERT INTO cards (card_type, topic, data, created_at) VALUES (?, ?, ?, ?)",
                          (card["card_type"], card["topic"], json.dumps(card), "2026-10-16T10:00:00"))
        con.commit()
        con.close()
        return cur.lastrowid

    def stored_quiz(self, card_id: int):
        con = connect()
        data = json.loads(con.execute("SELECT data FROM cards WHERE id = ?", (card_id,)).fetchone()[0])
        con.close()
        return data["quiz"]

    def claude(self, reply: dict):
        return mock.patch.object(app, "ask_claude", return_value=json.dumps(reply, ensure_ascii=False))

    def test_japanese_article_gets_japanese_quiz(self):
        card_id = self.add(article("ja"))
        with self.claude(JA_QUIZ) as ask:
            out = app.make_quiz(card_id)
        self.assertEqual(out["quiz"]["question"], JA_QUIZ["question"])
        self.assertEqual(self.stored_quiz(card_id), out["quiz"])
        prompt = ask.call_args.args[0]
        self.assertIn("Write everything in Japanese", prompt)
        self.assertIn('lang="ja"', prompt)

    def test_english_quiz_for_japanese_article_is_rejected(self):
        card_id = self.add(article("ja"))
        with self.claude(EN_QUIZ), self.assertRaises(HTTPException) as err:
            app.make_quiz(card_id)
        self.assertEqual(err.exception.status_code, 502)
        self.assertIsNone(self.stored_quiz(card_id))

    def test_english_article_gets_english_prompt(self):
        card_id = self.add(article("en"))
        with self.claude(EN_QUIZ) as ask:
            app.make_quiz(card_id)
        self.assertIn("Write in English", ask.call_args.args[0])

    def test_duplicate_options_are_rejected(self):
        card_id = self.add(article("en"))
        dup = {**EN_QUIZ, "options": ["UserDefaults", "The struct itself", " userdefaults ", "Keychain"]}
        with self.claude(dup) as ask, self.assertRaises(HTTPException) as err:
            app.make_quiz(card_id)
        self.assertEqual(err.exception.status_code, 502)
        self.assertEqual(ask.call_count, 1)  # no hidden retry
        self.assertIsNone(self.stored_quiz(card_id))

    def test_saved_quiz_is_returned_without_a_call(self):
        card_id = self.add(article("en", quiz=EN_QUIZ))
        with self.claude(JA_QUIZ) as ask:
            out = app.make_quiz(card_id)
        ask.assert_not_called()
        self.assertEqual(out["quiz"], EN_QUIZ)

    def test_second_request_reuses_the_first_quiz(self):
        card_id = self.add(article("ja"))
        with self.claude(JA_QUIZ) as ask:
            first = app.make_quiz(card_id)
            second = app.make_quiz(card_id)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
