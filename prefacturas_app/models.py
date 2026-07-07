from django.db import models


class GrupoArticuloCab(models.Model):
    id_grupo = models.AutoField(db_column="ID_GRUPO", primary_key=True)
    codigo = models.CharField(db_column="CODIGO", max_length=20, unique=True)
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=200)
    activo = models.CharField(db_column="ACTIVO", max_length=1, default="Y")
    fecha_creacion = models.DateTimeField(db_column="FECHA_CREACION", null=True, blank=True)
    fecha_act = models.DateTimeField(db_column="FECHA_ACT", null=True, blank=True)
    id_usuario = models.BigIntegerField(db_column="ID_USUARIO", null=True, blank=True)

    class Meta:
        db_table = "GRUPO_ARTICULO_CAB"


class GrupoArticuloDet(models.Model):
    id_det = models.AutoField(db_column="ID_DET", primary_key=True)
    id_grupo = models.ForeignKey(
        GrupoArticuloCab,
        db_column="ID_GRUPO",
        on_delete=models.CASCADE,
        related_name="detalles",
    )
    id_articulo = models.CharField(db_column="ID_ARTICULO", max_length=20)
    cantidad = models.DecimalField(db_column="CANTIDAD", max_digits=19, decimal_places=4, default=1)
    orden = models.IntegerField(db_column="ORDEN", default=0)

    class Meta:
        db_table = "GRUPO_ARTICULO_DET"
        indexes = [
            models.Index(fields=["id_grupo", "orden", "id_det"], name="IX_GRUPO_ART_DET_GRP"),
        ]


class EtiquetaFormatoUsuario(models.Model):
    id_config = models.AutoField(db_column="ID_CONFIG", primary_key=True)
    id_usuario = models.BigIntegerField(db_column="ID_USUARIO", unique=True)
    formato_json = models.TextField(db_column="FORMATO_JSON", default="{}")
    fecha_creacion = models.DateTimeField(db_column="FECHA_CREACION", auto_now_add=True)
    fecha_act = models.DateTimeField(db_column="FECHA_ACT", auto_now=True)

    class Meta:
        db_table = "ETIQUETA_FORMATO_USUARIO"


class CodigoVariable(models.Model):
    id_config = models.AutoField(db_column="ID_CONFIG", primary_key=True)
    prefijo = models.CharField(db_column="PREFIJO", max_length=10, unique=True)
    pos_producto = models.IntegerField(db_column="POS_PRODUCTO", default=2)
    len_producto = models.IntegerField(db_column="LEN_PRODUCTO", default=5)
    pos_valor = models.IntegerField(db_column="POS_VALOR", default=7)
    len_valor = models.IntegerField(db_column="LEN_VALOR", default=5)
    divisor_valor = models.DecimalField(db_column="DIVISOR_VALOR", max_digits=10, decimal_places=4, default=1000)
    tipo = models.CharField(db_column="TIPO", max_length=20, default="peso")  # "peso" o "precio"
    activo = models.CharField(db_column="ACTIVO", max_length=1, default="Y")

    class Meta:
        db_table = "CODIGO_VARIABLE"
