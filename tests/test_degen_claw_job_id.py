"""ACP stdout job id extraction."""

from src.acp.degen_claw import extract_acp_job_id


def test_extract_job_id_new_cli_block():
    stdout = """
Job Created
--------------------------------------------------
  Job ID             1003416033

  Job submitted. Use `acp job status <jobId>` to check progress.
""".strip()
    assert extract_acp_job_id(stdout) == "1003416033"


def test_extract_job_id_legacy_created():
    assert extract_acp_job_id("Job 1003224169 created") == "1003224169"


def test_extract_job_id_plain_job_number():
    assert extract_acp_job_id("something Job 42 other") == "42"


def test_extract_job_id_empty():
    assert extract_acp_job_id("") is None
    assert extract_acp_job_id("   ") is None


def test_extract_job_id_job_created_without_number():
    assert extract_acp_job_id("Job Created\nno id here") is None
