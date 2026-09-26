import argparse

from app import collections_collection, imports_collection, items_collection, users_collection


def main():
    parser = argparse.ArgumentParser(
        description="Assign pre-account Revisit records to an existing account."
    )
    parser.add_argument("username", help="Existing account that will own the legacy records")
    args = parser.parse_args()

    username_key = args.username.strip().casefold()
    user = users_collection.find_one({"username_key": username_key})
    if not user:
        parser.error("No account exists with that username.")

    unowned = {"user_id": {"$exists": False}}
    collections = {
        "items": items_collection,
        "collections": collections_collection,
        "imports": imports_collection,
    }
    counts = {
        name: collection.count_documents(unowned)
        for name, collection in collections.items()
    }
    total = sum(counts.values())
    print(f"Unowned records: {counts}")
    if not total:
        print("There are no unowned records to transfer.")
        return

    confirmation = input(
        f"Type {user['username']} to assign these records to that account: "
    )
    if confirmation != user["username"]:
        print("Transfer cancelled.")
        return

    owner_id = str(user["_id"])
    for collection in collections.values():
        collection.update_many(unowned, {"$set": {"user_id": owner_id}})
    print(f"Transferred {total} records to {user['username']}.")


if __name__ == "__main__":
    main()