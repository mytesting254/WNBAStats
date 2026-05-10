from __future__ import annotations


def seed_sample_data() -> None:
    raise RuntimeError(
        "Seed data has been removed. Use scripts/reset_live_db.py and the ESPN/The Odds API importers "
        "to build the local database from provider-backed data."
    )


if __name__ == "__main__":
    seed_sample_data()
