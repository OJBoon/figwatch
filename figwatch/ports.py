"""Repository protocols — ports for infrastructure access.

Structural (duck-typed) protocols. Implementations live in providers/.
"""

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CommentRepository(Protocol):
    """Port for reading and writing Figma comments."""

    def post_reply(self, file_key: str, parent_comment_id: str, message: str) -> Optional[str]:
        """Post a comment reply. Returns comment ID or None on failure."""
        ...

    def delete_comment(self, file_key: str, comment_id: str) -> None:
        """Delete a comment. Silent on failure."""
        ...

    def fetch_comments(self, file_key: str) -> list:
        """Fetch all comments for a file."""
        ...


@runtime_checkable
class DesignDataRepository(Protocol):
    """Port for fetching Figma design data needed by skills."""

    def fetch(self, required_data: list, file_key: str, node_id: str) -> tuple:
        """Fetch design data. Returns (data_dict, tree_data)."""
        ...
