"""The version hints that agents read must not steer them away from indexed releases.

Agents decide whether to call the tools from the server instructions and the
tool descriptions. A closed list that leaves out an indexed release (9.7) makes
them skip the tools for questions about it.
"""

import pytest

from percona_dk import mcp_remote_shim, mcp_server

HINTS = {
    "server instructions": mcp_server.mcp.instructions,
    "server search_percona_docs": mcp_server.search_percona_docs.__doc__,
    "shim search_percona_docs": mcp_remote_shim.search_percona_docs.__doc__,
}


@pytest.mark.parametrize("name", HINTS)
def test_hints_name_the_9_7_release(name: str) -> None:
    assert "9.7" in HINTS[name]


@pytest.mark.parametrize("name", HINTS)
def test_hints_do_not_claim_mysql_is_only_8_0_and_8_4(name: str) -> None:
    text = " ".join(HINTS[name].split())
    assert "indexed on 8.0 and 8.4" not in text
    assert 'PDMySQL: "8.0", "8.4"\n' not in HINTS[name]
    assert "PXB 9.x" not in text
