from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ajustes", "0002_facturacion_electronica"),
    ]

    operations = [
        migrations.CreateModel(
            name="UsuarioFirma",
            fields=[
                ("id_firma", models.AutoField(db_column="ID_FIRMA", primary_key=True, serialize=False)),
                ("id_usuario", models.BigIntegerField(db_column="ID_USUARIO", unique=True)),
                ("firma", models.BinaryField(blank=True, db_column="FIRMA", null=True)),
                ("creado_en", models.DateTimeField(auto_now_add=True, db_column="CREADO_EN")),
                ("actualizado_en", models.DateTimeField(auto_now=True, db_column="ACTUALIZADO_EN")),
            ],
            options={
                "db_table": "SEG_USUARIO_FIRMA",
                "ordering": ["id_usuario"],
            },
        ),
        migrations.RunSQL(
            sql="""
            IF COL_LENGTH('USUARIO', 'ID_FIRMA') IS NULL
            BEGIN
                ALTER TABLE USUARIO ADD ID_FIRMA INT NULL;
            END
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
