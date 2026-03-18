import json

from django.core.management.base import BaseCommand, CommandError

from factura.ecf_snapshot import build_document_snapshot


class Command(BaseCommand):
    help = "Imprime el payload base del documento que se enviaria a un integrador e-CF."

    def add_arguments(self, parser):
        parser.add_argument("id_doc", type=int)

    def handle(self, *args, **options):
        id_doc = int(options["id_doc"])
        try:
            payload = build_document_snapshot(id_doc)
        except Exception as exc:
            raise CommandError(str(exc))
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
