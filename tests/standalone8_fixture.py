"""Run local standalone-skill fixtures inside the installed Linux agent image."""

import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

ROOT = Path("/opt/agent-bundle/standalone-skills")
NAMES = ("cdc-check-and-update", "etl-task-solver", "lmd-db-state-check",
         "postgre-sql-code-style", "project-context-reload",
         "skill-plugin-marketplace-workflow", "work-tools", "xlsx-workflow")


def run(*args, expected=0):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode != expected:
        raise AssertionError("local skill command failed: " + " ".join(map(str, args[:3])))
    return result


def fixture_xlsx(path):
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook = (f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                '<sheet name="Mapping" sheetId="1" r:id="rId1"/></sheets></workbook>')
    relationships = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                     'Target="worksheets/sheet1.xml"/></Relationships>')
    def cell(ref, value):
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
    sheet = (f'<worksheet xmlns="{main}"><dimension ref="A1:C2"/><sheetData>'
             f'<row r="1">{cell("A1", "Target table name")}{cell("B1", "Target column name")}{cell("C1", "Value")}</row>'
             f'<row r="2">{cell("A2", "dds.anchor")}{cell("B2", "id")}<c r="C2"><f>1+2</f><v>3</v></c></row>'
             '</sheetData></worksheet>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def main():
    resource_counts = {}
    for name in NAMES:
        folder = ROOT / name
        skill = folder / "SKILL.md"
        assert skill.is_file() and skill.stat().st_size > 100
        assert f"name: {name}" in skill.read_text().split("---", 2)[1]
        resource_counts[name] = len([p for p in folder.rglob("*") if p.is_file()])
    assert len(resource_counts) == 8
    for relative in ("SKILL.md", "references/s2t-excel/discovery.md"):
        guidance = (ROOT / "lmd-db-state-check" / relative).read_text()
        assert "$CODEX_HOME/skills/lmd-db-state-check/artifacts/s2t-xlsx/scripts/download_s2t_attachment.py" in guidance
        assert "--output-directory /workspace/task/update" in guidance or relative == "SKILL.md"
    with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
        stage = Path(temporary)
        yaml = stage / "cdc.yaml"
        yaml.write_text("columns:\n  - field_name: ALPHA\n  - field_name: BETA\n")
        cdc = ROOT / "cdc-check-and-update/scripts/check_cdc_yaml_fields.py"
        checked = json.loads(run("python3", str(cdc), str(yaml), "--field", "BETA").stdout)
        assert checked[0]["is_sorted"] and checked[0]["duplicate_count"] == 0
        yaml.write_text("columns:\n  - field_name: BETA\n  - field_name: ALPHA\n  - field_name: ALPHA\n")
        rejected = json.loads(run("python3", str(cdc), str(yaml)).stdout)
        assert not rejected[0]["is_sorted"] and rejected[0]["duplicate_count"] == 1

        workbook = stage / "source.xlsx"
        fixture_xlsx(workbook)
        helper_path = ROOT / "lmd-db-state-check/artifacts/s2t-xlsx/scripts/download_s2t_attachment.py"
        module_spec = importlib.util.spec_from_file_location("s2t_linux", helper_path)
        s2t = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(s2t)
        profile = {"CONFLUENCE_BASE_URL": "https://confluence.example.test",
                   "CONFLUENCE_API_TOKEN": "synthetic"}
        hostile = stage / "untrusted.env"
        hostile.write_text("CONFLUENCE_BASE_URL=https://foreign.example.test\n")
        try:
            s2t.config({"CONFLUENCE_API_TOKEN": "trusted-synthetic"}, hostile)
        except ValueError as error:
            assert "same trusted source" in str(error)
        else:
            raise AssertionError("trusted token was combined with untrusted origin")
        assert s2t.config(profile, hostile)[0] == "https://confluence.example.test"
        payload = {"results": [{"title": "dds_S2T_mapping_v9.xlsx", "id": "old"},
            {"title": "dds_S2T_mapping_v104.xlsx", "id": "new",
             "version": {"number": 2, "when": "synthetic"},
             "_links": {"download": "/download/104"}}]}
        def fake_fetch(url, authorization, limit):
            assert url.startswith("https://confluence.example.test/")
            assert authorization == "Bearer synthetic"
            return workbook.read_bytes() if url.endswith("/download/104") else json.dumps(payload).encode()
        listed = s2t.attachment("205225775", stage, True, env=profile, fetcher=fake_fetch)
        assert listed["business_version"] == 104 and "output_path" not in listed
        downloaded = s2t.attachment("205225775", stage, False, env=profile, fetcher=fake_fetch)
        assert Path(downloaded["output_path"]).read_bytes() == workbook.read_bytes()
        payload["_links"] = {"next": "/rest/api/content/205225775/child/attachment?start=100"}
        try:
            s2t.attachment("205225775", stage, True, env=profile, fetcher=fake_fetch)
        except ValueError as error:
            assert "incomplete" in str(error)
        else:
            raise AssertionError("partial attachment list was accepted")
        payload.pop("_links")
        payload["results"][1]["_links"]["download"] = "//foreign.example.test/path"
        try:
            s2t.attachment("205225775", stage, False, env=profile, fetcher=fake_fetch)
        except ValueError:
            pass
        else:
            raise AssertionError("cross-origin attachment link was accepted")
        audit = ROOT / "xlsx-workflow/scripts/xlsx_audit.py"
        run("python3", str(audit), "zip-test", str(workbook))
        assert "Mapping" in run("python3", str(audit), "sheets", str(workbook)).stdout
        run("python3", str(audit), "coverage", str(workbook), "--sheet", "Mapping",
            "--range", "C2:C2", "--require-formulas")
        assert "1+2" in run("python3", str(audit), "dump", str(workbook),
                            "--sheet", "Mapping", "--range", "C2:C2").stdout

        scripts = ROOT / "lmd-db-state-check/artifacts/s2t-xlsx/scripts"
        csv_out = stage / "csv"
        run("python3", str(scripts / "export_xlsx_csv.py"), str(workbook), str(csv_out), "Mapping")
        with (csv_out / "Mapping.csv").open(newline="") as stream:
            rows = list(csv.reader(stream, delimiter=";"))
        assert rows[0][:2] == ["Target table name", "Target column name"]
        assert rows[1][:2] == ["dds.anchor", "id"]

        spec = {"version": 1, "workbook": {"source": "source.xlsx", "target": "updated.xlsx"},
                "blocks": [{"id": "dds.new", "sheet": "Mapping", "header_row": 1,
                    "match": {"column": "Target table name", "value": "dds.new"},
                    "insert": {"mode": "after", "column": "Target table name", "value": "dds.anchor"},
                    "style": {"mode": "copy_from_anchor"},
                    "rows": [{"Target table name": "dds.new", "Target column name": "id"}]}]}
        spec_path = stage / "spec.json"
        spec_path.write_text(json.dumps(spec))
        apply = scripts / "apply_s2t_blocks.py"
        denied = run("python3", str(apply), str(spec_path), "--task-dir", str(stage), expected=2)
        assert "--skip-excel-validation" in denied.stderr and not (stage / "updated.xlsx").exists()
        run("python3", str(apply), str(spec_path), "--task-dir", str(stage), "--skip-excel-validation")
        run("python3", str(audit), "zip-test", str(stage / "updated.xlsx"))
        run("python3", str(scripts / "export_xlsx_csv.py"), str(stage / "updated.xlsx"),
            str(csv_out), "Mapping")
        assert "dds.new" in (csv_out / "Mapping.csv").read_text()

        check = ROOT / "work-tools/scripts/linux-runtime-check.sh"
        run("sh", str(check), "local")
        unavailable = {}
        for mode in ("sqlserver", "kafka", "helpdesk", "excel"):
            result = subprocess.run(["sh", str(check), mode], text=True, capture_output=True)
            unavailable[mode] = result.returncode
            assert result.returncode == 2
    print(json.dumps({"skills": resource_counts, "localScripts": 6,
                      "cdcSortedAndDuplicate": True, "xlsxAuditAndExport": True,
                      "s2tExplicitOoxmlFallback": True,
                      "confluenceMockListDownload": True, "externalModes": unavailable}))


if __name__ == "__main__":
    main()
