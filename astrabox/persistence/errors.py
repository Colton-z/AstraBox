"""Relational constraint failures shared by repositories and providers."""

class UniqueViolation(Exception):
    """A row with the same unique key already exists."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(f"unique constraint {constraint!r} violated")
        self.constraint = constraint

class ForeignKeyViolation(Exception):
    """The statement referenced a row that does not exist, or removed a referenced row."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(f"foreign key constraint {constraint!r} violated")
        self.constraint = constraint
