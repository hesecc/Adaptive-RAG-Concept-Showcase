"""Trusted local management CLI. HTTP authentication is a separate adapter."""

import argparse
import asyncio
from dataclasses import asdict, is_dataclass, replace
import json
from pathlib import Path
import sys

from .app import RagApplication
from .config import DEFAULT_PLUGINS, load_settings
from .domain import AccessContext, IngestRequest, QueryRequest, RagError, ValidationError
from .evaluation import run_evaluation
from .evaluation_reports import ReportStore
from .environment import apply_environment, pdf_settings, read_environment
from .permission_matrix import failures, run_permission_matrix


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValidationError(message)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(prog="adaptive-rag", description="Pluggable RAG with explicit cloud opt-in")
    root.add_argument("--data-dir", type=Path)
    root.add_argument("--config", type=Path)
    root.add_argument("--env-file", type=Path, help="Explicit local credentials file; never loaded implicitly")
    commands = root.add_subparsers(dest="command", required=True)

    def tenant_command(name, **kwargs):
        command = commands.add_parser(name, **kwargs)
        command.add_argument("--tenant", required=True)
        return command

    command = tenant_command("ingest")
    command.add_argument("--document", required=True)
    command.add_argument("--version", required=True)
    command.add_argument("--file", required=True, type=Path)
    command.add_argument("--title")
    command.add_argument("--sku", action="append", default=[])
    command.add_argument("--allow", action="append", default=[])
    command.add_argument("--data-domain", action="append", default=[], help="Data classification, e.g. operations or finance; repeat for several. Without it the document is unclassified and only administrators can read it. Ignored when the document already exists")
    command.add_argument("--language", default="zh")
    command.add_argument("--pdf-backend", choices=("local", "mineru"))
    command.add_argument("--pdf-checkpoint", type=Path, help="MinerU-only batch checkpoint for PDF ingestion")

    command = commands.add_parser("parse-pdf")
    command.add_argument("--file", required=True, type=Path)
    command.add_argument("--backend", choices=("local", "mineru"))
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--checkpoint", type=Path, help="MinerU-only local checkpoint; resume the same batch after failure")

    command = tenant_command("publish")
    command.add_argument("--job", required=True)
    command.add_argument("--expected-generation")

    command = tenant_command("candidate-create", help="Build a candidate without changing the live generation")
    command.add_argument("--job", required=True, action="append", help="Staged job ID; repeat for a batch")
    command.add_argument("--expected-generation", help="Live base generation; omit only for an empty tenant")
    for name, help_text in (
        ("candidate-status", "Inspect candidate state"),
        ("candidate-prewarm", "Plan candidate prewarming; no embedding calls unless --execute is given"),
        ("prewarm", "Plan published-generation prewarming; use --execute to populate the dense cache"),
        ("candidate-preview", "Preview candidate evidence through the trusted model-free interface"),
        ("candidate-activate", "Activate a candidate using the expected live generation"),
        ("candidate-abandon", "Discard a pending candidate and release its staged jobs"),
    ):
        command = tenant_command(name, help=help_text)
        command.add_argument("--generation", required=True)
        if name in {"prewarm", "candidate-prewarm"}:
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true", help="Plan only (default)")
            mode.add_argument("--execute", action="store_true", help="Perform prewarming; may incur embedding charges")
            command.add_argument("--max-chunks", type=int, help="Maximum missing cache keys to process in this run")
            command.add_argument("--all-scopes", action="store_true", help="Trusted operator: include all non-revoked documents and SKUs; cannot combine with identity or SKU filters")
        elif name == "candidate-preview":
            command.add_argument("--question", required=True)
            command.add_argument("--top-k", type=int, default=5)
        elif name == "candidate-activate":
            command.add_argument("--expected-generation", help="Live base generation; omit only for an empty tenant")
        if name in {"prewarm", "candidate-prewarm", "candidate-preview"}:
            command.add_argument("--sku")
            command.add_argument("--subject", default=None, help="Subject for a scoped run (default: local)")
            command.add_argument("--group", action="append", default=[])
            command.add_argument("--data-domain", action="append", default=[])

    command = tenant_command("query")
    command.add_argument("--question", required=True)
    command.add_argument("--sku")
    command.add_argument("--generation")
    command.add_argument("--top-k", type=int, default=5)
    command.add_argument("--subject", default="local")
    command.add_argument("--group", action="append", default=[])

    commands.add_parser("capabilities")
    for name in ("job", "trace"):
        command = tenant_command(name)
        command.add_argument("--id", required=True)
    command = tenant_command("evidence")
    command.add_argument("--generation", required=True)
    command.add_argument("--id", required=True)
    command.add_argument("--subject", default="local")
    command.add_argument("--group", action="append", default=[])

    command = tenant_command("delete")
    command.add_argument("--document", required=True)
    command.add_argument("--expected-generation")
    command = tenant_command("rollback")
    command.add_argument("--generation", required=True)
    command.add_argument("--expected-generation")
    command = tenant_command("access")
    command.add_argument("--document", required=True)
    command.add_argument("--allow", action="append", default=[])
    command.add_argument("--revoked", action="store_true")

    command = tenant_command("eval", help="Retrieval metrics on a gold fixture; the report is saved for the evaluation page")
    command.add_argument("--fixture", required=True, type=Path)
    command.add_argument("--output", type=Path, help="Also write a copy of the report here")
    command.add_argument("--generation")
    command.add_argument("--top-k", type=int, default=5)
    command.add_argument("--subject", default="local")
    command.add_argument("--group", action="append", default=[])
    command.add_argument("--data-domain", action="append", default=[], help="Evaluate as an identity limited to these data classifications; repeat for several. Without it the run is unrestricted, like the other local commands")
    command = tenant_command("eval-permissions", help="Check which documents each identity can retrieve, on a synthetic corpus with no model calls; exits 3 on any leak or miss")
    command.add_argument("--output", type=Path, help="Also write a copy of the full report here")
    commands.add_parser("serve")
    command = tenant_command("assets-index", help="Process durable image-description indexing tasks")
    command.add_argument("--limit", type=int, default=10)
    return root


def _access(args) -> AccessContext:
    # An explicit data-domain selection simulates the restricted identity in a local run.
    domains = getattr(args, "data_domain", None)
    return AccessContext(args.tenant, "local" if args.subject is None else args.subject,
                         tuple(args.group), tuple(domains) if domains else None)


def _save_report(settings, report, kind, output):
    saved = ReportStore(settings.data_dir).save(report, kind)
    if output is not None:
        try:
            output.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ValidationError("Cannot write evaluation output") from exc
    return saved


def dispatch(args):
    if getattr(args, "all_scopes", False) and (
        args.subject is not None or args.group or args.data_domain or args.sku is not None
    ):
        raise ValidationError("--all-scopes cannot be combined with --subject, --group, --data-domain or --sku")
    environment = read_environment(args.env_file)
    if args.command == "parse-pdf":
        from .pdf import parse_pdf
        output = args.output.resolve()
        if output == args.file.resolve():
            raise ValidationError("PDF output cannot replace the source file")
        if args.checkpoint is not None and output == args.checkpoint.resolve():
            raise ValidationError("PDF output cannot replace the checkpoint file")
        parsed = parse_pdf(args.file, args.backend or environment.get("RAG_PDF_BACKEND", "local"), pdf_settings(environment),
                           checkpoint_path=args.checkpoint)
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(parsed.markdown, encoding="utf-8")
        except OSError:
            raise ValidationError("cannot write PDF Markdown output") from None
        return {"backend": parsed.backend, "pages": parsed.pages, "characters": len(parsed.markdown),
                "duration_seconds": parsed.duration_seconds, "output": str(args.output), "metadata": parsed.metadata}
    settings = apply_environment(load_settings(args.config), environment)
    if args.data_dir is not None:
        settings = replace(settings, data_dir=args.data_dir)
    if args.command == "serve":
        from .api import create_api
        import uvicorn
        api = create_api(settings, mineru_settings=pdf_settings(environment))
        uvicorn.run(api, host=settings.host, port=settings.port, access_log=False)
        return {"status": "stopped"}
    if args.command == "eval-permissions":
        # The matrix builds its own model-free application on a synthetic corpus and never
        # the one the environment configures, so it cannot spend model calls.
        with ReportStore(settings.data_dir).scratch() as work_dir:
            report = asyncio.run(run_permission_matrix(args.tenant, work_dir))
        saved = _save_report(settings, report, "permissions", args.output)
        return {"report_id": saved["report_id"], "kind": "permissions", "passed": saved["summary"]["passed"],
                "summary": saved["summary"], "failures": failures(saved)}
    if args.command == "candidate-preview":
        # Keep configured storage, but never run cloud query plugins during operator preview.
        storage = {role: settings.plugins[role] for role in ("store", "objects", "trace")}
        settings = replace(settings, plugins=DEFAULT_PLUGINS | storage, allow_models=False)
    application = RagApplication(settings)
    if args.command == "candidate-create":
        return application.store.create_candidate_snapshot(args.tenant, tuple(args.job), args.expected_generation)
    if args.command == "candidate-activate":
        return application.store.activate_candidate_snapshot(args.tenant, args.generation, args.expected_generation)
    if args.command == "candidate-abandon":
        removed = application.store.abandon_candidate_snapshot(args.tenant, args.generation)
        return {"generation": args.generation, "abandoned": removed}
    if args.command == "candidate-status":
        snapshot = application.store.open_snapshot(args.tenant, args.generation, allow_candidate=True)
        live = application.store.open_snapshot(args.tenant)
        return {"generation": snapshot.generation, "revisions": snapshot.revisions,
                "live_generation": live.generation if live else None,
                "unactivated": application.store.is_candidate_snapshot(args.tenant, args.generation)}
    if args.command == "candidate-preview":
        return asyncio.run(application.preview_candidate(QueryRequest(_access(args), args.question, sku=args.sku,
                                                                     generation=args.generation, top_k=args.top_k)))
    if args.command in {"prewarm", "candidate-prewarm"}:
        return asyncio.run(application.prewarm(_access(args), args.generation, sku=args.sku, dry_run=not args.execute,
                                              max_chunks=args.max_chunks, candidate=args.command == "candidate-prewarm",
                                              all_scopes=args.all_scopes))
    if args.command == "assets-index":
        return asyncio.run(application.assets.process_pending(args.tenant, args.limit))
    if args.command == "ingest":
        if args.file.suffix.lower() == ".pdf":
            return application.ingest_pdf(args.file, backend=args.pdf_backend or environment.get("RAG_PDF_BACKEND", "local"),
                pdf_settings=pdf_settings(environment), pdf_checkpoint=args.pdf_checkpoint, tenant_id=args.tenant, document_id=args.document, version=args.version,
                title=args.title or args.file.stem, scope="sku" if args.sku else "store", sku_ids=tuple(args.sku),
                allowed_subjects=tuple(args.allow), language=args.language, data_domains=tuple(args.data_domain))
        media_type = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
                      ".html": "text/html", ".htm": "text/html"}.get(args.file.suffix.lower())
        if media_type is None:
            raise ValidationError("Supported source extensions: .md, .markdown, .txt, .html, .htm, .pdf")
        try:
            if args.file.stat().st_size > 8_000_000:
                raise ValidationError("Source file exceeds the 8 MB input limit")
            content = args.file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError("Cannot read source file as UTF-8") from exc
        return application.ingest(IngestRequest(
            tenant_id=args.tenant, document_id=args.document, version=args.version, content=content,
            media_type=media_type, title=args.title or args.file.stem,
            scope="sku" if args.sku else "store", sku_ids=tuple(args.sku),
            allowed_subjects=tuple(args.allow), language=args.language,
            data_domains=tuple(args.data_domain),
        ))
    if args.command == "publish":
        return application.publish(args.tenant, args.job, args.expected_generation)
    if args.command == "query":
        return asyncio.run(application.query(QueryRequest(_access(args), args.question, sku=args.sku, generation=args.generation, top_k=args.top_k)))
    if args.command == "capabilities":
        return application.capabilities()
    if args.command == "job":
        return application.get_job(args.tenant, args.id)
    if args.command == "trace":
        return application.get_trace(args.tenant, args.id)
    if args.command == "evidence":
        return application.get_evidence(_access(args), args.generation, args.id)
    if args.command == "delete":
        return application.delete_document(args.tenant, args.document, args.expected_generation)
    if args.command == "rollback":
        return application.rollback(args.tenant, args.generation, args.expected_generation)
    if args.command == "access":
        application.set_access(args.tenant, args.document, tuple(args.allow), args.revoked)
        return {"status": "updated"}
    if args.command == "eval":
        report = asyncio.run(run_evaluation(application, args.fixture, _access(args), generation=args.generation, top_k=args.top_k))
        return _save_report(settings, report, "retrieval", args.output)
    raise ValidationError("Unknown command")


def main(argv: list[str] | None = None) -> int:
    try:
        result = dispatch(parser().parse_args(argv))
        print(json.dumps(asdict(result) if is_dataclass(result) else result, ensure_ascii=False))
        # 3: the run finished but a check failed (eval-permissions found a leak or a miss).
        return 3 if isinstance(result, dict) and result.get("passed") is False else 0
    except RagError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": {"code": "internal_error", "message": "Internal application error"}}), file=sys.stderr)
        return 1
