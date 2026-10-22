"""An enterprise policy selected only by the isolated entry-policy deployment."""

from astrabox.common.utils.user_context import UserContext


class CorporateEntryPolicy:
    async def can_enter(self, user: UserContext, *, environment_name: str, operation: str) -> bool:
        if user.org_id == "directory-unavailable":
            raise RuntimeError("the fixture directory is unavailable")
        return user.org_id == "employees"
