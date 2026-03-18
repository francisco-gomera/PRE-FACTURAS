from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("ajustes", "0003_usuario_firma"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            IF COL_LENGTH('USUARIO', 'FIRMA') IS NOT NULL
            BEGIN
                ALTER TABLE USUARIO DROP COLUMN FIRMA;
            END
            """,
            reverse_sql="""
            IF COL_LENGTH('USUARIO', 'FIRMA') IS NULL
            BEGIN
                ALTER TABLE USUARIO ADD FIRMA VARBINARY(MAX) NULL;
            END
            """,
        ),
    ]
