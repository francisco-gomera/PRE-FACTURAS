from django.core.management.base import BaseCommand

from factura.ecf_runtime import build_ecf_runtime_report


class Command(BaseCommand):
    help = "Audita el readiness tecnico del proyecto para e-CF."

    def handle(self, *args, **options):
        report = build_ecf_runtime_report()
        self.stdout.write(f"Modo integracion: {report['provider_mode']}")
        self.stdout.write(f"Listo para precertificacion: {'SI' if report['ready_for_precertificacion'] else 'NO'}")
        self.stdout.write("")
        for item in report["checks"]:
            marker = "[OK]" if item["ok"] else "[NO]"
            self.stdout.write(f"{marker} {item['label']}")
            self.stdout.write(f"     {item['detail']}")
