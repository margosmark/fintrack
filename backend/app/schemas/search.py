"""search — global fuzzy lookup shapes (docs/08)."""

from pydantic import BaseModel

from app.schemas.transactions import SimilarTransaction


class CategoryHit(BaseModel):
    id: int
    name: str


class AccountHit(BaseModel):
    id: int
    name: str
    type: str


class SecurityHit(BaseModel):
    id: int
    symbol: str
    name: str
    kind: str
    exchange: str


class TagHit(BaseModel):
    id: int
    name: str


class SearchResults(BaseModel):
    query: str
    transactions: list[SimilarTransaction]
    # M137: matches left out only because `hide_encrypted` was set (a locked
    # vault). 0 unless asked — the payload is otherwise unchanged.
    withheld: int = 0
    categories: list[CategoryHit]
    accounts: list[AccountHit]
    securities: list[SecurityHit] = []
    tags: list[TagHit] = []
