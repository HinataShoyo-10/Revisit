import os
import re
import unittest
from unittest.mock import patch

os.environ["MONGODB_URI"] = "mongodb://localhost/revisit-tests"
os.environ["SECRET_KEY"] = "test-only-session-key"
os.environ["SESSION_COOKIE_SECURE"] = "false"

import mongomock
import pymongo

with patch.object(pymongo, "MongoClient", mongomock.MongoClient):
    import app as revisit


class AccountTests(unittest.TestCase):
    def setUp(self):
        revisit.mongo_client.drop_database(revisit.database.name)
        revisit.init_db()
        self.alice = revisit.app.test_client()
        self.bob = revisit.app.test_client()

    def csrf_token(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def register(self, client, username, email, pin):
        token = self.csrf_token(client, "/register")
        return client.post(
            "/register",
            data={
                "csrf_token": token,
                "username": username,
                "email": email,
                "password": pin,
            },
            follow_redirects=True,
        )

    def test_username_and_email_are_unique_case_insensitively(self):
        self.assertEqual(
            self.register(self.alice, "alice", "alice@example.com", "1234").status_code,
            200,
        )
        duplicate_username = revisit.app.test_client()
        self.assertEqual(
            self.register(
                duplicate_username, "Alice", "different@example.com", "5678"
            ).status_code,
            409,
        )
        duplicate_email = revisit.app.test_client()
        self.assertEqual(
            self.register(
                duplicate_email, "carol", "ALICE@example.com", "5678"
            ).status_code,
            409,
        )

    def test_login_and_csrf_protection(self):
        self.register(self.alice, "alice", "alice@example.com", "1234")
        client = revisit.app.test_client()
        token = self.csrf_token(client, "/login")
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": "alice", "password": "1234"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post("/logout").status_code, 400)

    def test_accounts_cannot_read_or_delete_each_others_items(self):
        self.register(self.alice, "alice", "alice@example.com", "1234")
        self.register(self.bob, "bob", "bob@example.com", "5678")
        alice_record = revisit.users_collection.find_one({"username_key": "alice"})
        bob_record = revisit.users_collection.find_one({"username_key": "bob"})
        revisit.items_collection.insert_many([
            {
                "_id": "alice-item",
                "user_id": alice_record["_id"],
                "url": "https://example.com/alice",
                "platform": "other",
                "title": "Alice private item",
                "tags": [],
                "collections": [],
                "added_at": "2026-01-01",
            },
            {
                "_id": "bob-item",
                "user_id": bob_record["_id"],
                "url": "https://example.com/bob",
                "platform": "other",
                "title": "Bob private item",
                "tags": [],
                "collections": [],
                "added_at": "2026-01-02",
            },
        ])

        library = self.alice.get("/").get_data(as_text=True)
        self.assertIn("Alice private item", library)
        self.assertNotIn("Bob private item", library)
        token = re.search(r'name="csrf_token" value="([^"]+)"', library).group(1)
        self.alice.post("/item/bob-item/delete", data={"csrf_token": token})
        self.assertIsNotNone(revisit.items_collection.find_one({"_id": "bob-item"}))


if __name__ == "__main__":
    unittest.main()