"""Document store keyed by (kind, id), with a local JSON backend and a DynamoDB backend.

DynamoDB layout (single table): pk = patient id, sk = "<kind>#<id>", data = JSON document.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .config import settings

M = TypeVar("M", bound=BaseModel)


class Store(ABC):
    @abstractmethod
    def put(self, kind: str, id: str, doc: dict) -> None: ...

    @abstractmethod
    def get(self, kind: str, id: str) -> dict | None: ...

    @abstractmethod
    def list(self, kind: str) -> list[dict]: ...

    @abstractmethod
    def delete(self, kind: str, id: str) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    # typed helpers
    def save(self, kind: str, model: BaseModel, id: str | None = None) -> None:
        self.put(kind, id or getattr(model, "id"), model.model_dump(mode="json"))

    def load(self, kind: str, id: str, cls: type[M]) -> M | None:
        doc = self.get(kind, id)
        return cls.model_validate(doc) if doc else None

    def load_all(self, kind: str, cls: type[M]) -> list[M]:
        return [cls.model_validate(d) for d in self.list(kind)]


class JsonStore(Store):
    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict]] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text() or "{}")

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1))
        tmp.replace(self._path)

    def put(self, kind, id, doc):
        with self._lock:
            self._data.setdefault(kind, {})[id] = doc
            self._flush()

    def get(self, kind, id):
        return self._data.get(kind, {}).get(id)

    def list(self, kind):
        return list(self._data.get(kind, {}).values())

    def delete(self, kind, id):
        with self._lock:
            self._data.get(kind, {}).pop(id, None)
            self._flush()

    def clear(self):
        with self._lock:
            self._data = {}
            self._flush()


class DynamoStore(Store):
    def __init__(self, table: str, pk: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table)
        self._pk = pk

    def put(self, kind, id, doc):
        self._table.put_item(Item={"pk": self._pk, "sk": f"{kind}#{id}", "data": json.dumps(doc)})

    def get(self, kind, id):
        item = self._table.get_item(Key={"pk": self._pk, "sk": f"{kind}#{id}"}).get("Item")
        return json.loads(item["data"]) if item else None

    def list(self, kind):
        from boto3.dynamodb.conditions import Key

        out, kwargs = [], {"KeyConditionExpression": Key("pk").eq(self._pk) & Key("sk").begins_with(f"{kind}#")}
        while True:
            page = self._table.query(**kwargs)
            out += [json.loads(i["data"]) for i in page["Items"]]
            if "LastEvaluatedKey" not in page:
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def delete(self, kind, id):
        self._table.delete_item(Key={"pk": self._pk, "sk": f"{kind}#{id}"})

    def clear(self):
        from boto3.dynamodb.conditions import Key

        kwargs = {"KeyConditionExpression": Key("pk").eq(self._pk), "ProjectionExpression": "pk, sk"}
        with self._table.batch_writer() as batch:
            while True:
                page = self._table.query(**kwargs)
                for item in page["Items"]:
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                if "LastEvaluatedKey" not in page:
                    return
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def make_store() -> Store:
    if settings.store == "dynamodb":
        return DynamoStore(settings.dynamodb_table, settings.patient_id, settings.aws_region)
    return JsonStore(settings.data_file)
