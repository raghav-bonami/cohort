from pymongo import AsyncMongoClient


def create_mongo_client(url: str) -> AsyncMongoClient:
    return AsyncMongoClient(url, serverSelectionTimeoutMS=2000, tz_aware=True)
