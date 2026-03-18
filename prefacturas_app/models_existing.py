# This is an auto-generated Django model module.
# You'll have to do the following manually to clean this up:
#   * Rearrange models' order
#   * Make sure each model has one field with primary_key=True
#   * Make sure each ForeignKey and OneToOneField has `on_delete` set to the desired behavior
#   * Remove `managed = False` lines if you wish to allow Django to create, modify, and delete the table
# Feel free to rename the models, but don't rename db_table values or field names.
from django.db import models


class CabPedido(models.Model):
    id_doc = models.DecimalField(db_column='ID_DOC', primary_key=True, max_digits=18, decimal_places=0)  # Field name made lowercase.
    id_doc_base = models.DecimalField(db_column='ID_DOC_BASE', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    tipo_doc_base = models.CharField(db_column='TIPO_DOC_BASE', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cancelado = models.CharField(db_column='CANCELADO', max_length=15, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    impreso = models.CharField(db_column='IMPRESO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    est_doc = models.CharField(db_column='EST_DOC', max_length=15, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo_doc = models.CharField(db_column='TIPO_DOC', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    contacto = models.CharField(db_column='CONTACTO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    fecha_cont = models.DateTimeField(db_column='FECHA_CONT', blank=True, null=True)  # Field name made lowercase.
    fecha_doc = models.DateTimeField(db_column='FECHA_DOC', blank=True, null=True)  # Field name made lowercase.
    fecha_venc = models.DateTimeField(db_column='FECHA_VENC', blank=True, null=True)  # Field name made lowercase.
    fecha_act = models.CharField(db_column='FECHA_ACT', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    fecha_ent = models.CharField(db_column='FECHA_ENT', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    fecha_dev = models.CharField(db_column='FECHA_DEV', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_sn = models.CharField(db_column='ID_SN', max_length=12, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_socio = models.CharField(db_column='NOM_SOCIO', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    rnc_ced = models.CharField(db_column='RNC_CED', max_length=13, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ent_factura = models.CharField(db_column='ENT_FACTURA', max_length=200, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ent_mercancia = models.CharField(db_column='ENT_MERCANCIA', max_length=200, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    referencia = models.CharField(db_column='REFERENCIA', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    subtotal = models.DecimalField(db_column='SUBTOTAL', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_desc = models.DecimalField(db_column='TOTAL_DESC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_itbis = models.DecimalField(db_column='TOTAL_ITBIS', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_doc = models.DecimalField(db_column='TOTAL_DOC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    mon_doc = models.CharField(db_column='MON_DOC', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    abono = models.DecimalField(db_column='ABONO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    saldo = models.DecimalField(db_column='SALDO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_doc2 = models.DecimalField(db_column='TOTAL_DOC2', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    abono2 = models.DecimalField(db_column='ABONO2', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    saldo2 = models.DecimalField(db_column='SALDO2', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    tasa = models.DecimalField(db_column='TASA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    comentario = models.CharField(db_column='COMENTARIO', max_length=500, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    no_ed = models.DecimalField(db_column='NO_ED', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    no_recibo = models.DecimalField(db_column='NO_RECIBO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    id_condicion = models.DecimalField(db_column='ID_CONDICION', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    dia = models.DecimalField(db_column='DIA', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    condicion = models.CharField(db_column='CONDICION', max_length=15, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_vendedor = models.DecimalField(db_column='ID_VENDEDOR', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    fecha_creacion = models.DateTimeField(db_column='FECHA_CREACION', blank=True, null=True)  # Field name made lowercase.
    id_ncf = models.DecimalField(db_column='ID_NCF', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    ncf = models.CharField(db_column='NCF', max_length=21, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ncf_nc = models.CharField(db_column='NCF_NC', max_length=21, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo = models.CharField(db_column='TIPO', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    serie = models.CharField(db_column='SERIE', max_length=9, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    periodo_cont = models.CharField(db_column='PERIODO_CONT', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_usuario = models.DecimalField(db_column='ID_USUARIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    total_base = models.DecimalField(db_column='TOTAL_BASE', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cta_asociada = models.CharField(db_column='CTA_ASOCIADA', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ejercicio = models.DecimalField(db_column='EJERCICIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    total_anticipo = models.DecimalField(db_column='TOTAL_ANTICIPO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_gasto = models.DecimalField(db_column='ID_GASTO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    tipo_gasto = models.CharField(db_column='TIPO_GASTO', max_length=80, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    terminal = models.CharField(db_column='TERMINAL', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_precio = models.DecimalField(db_column='ID_PRECIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    fechavencncf = models.DateTimeField(db_column='FechaVencNCF', blank=True, null=True)  # Field name made lowercase.
    aprobacion = models.CharField(db_column='APROBACION', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    autorizado = models.CharField(db_column='AUTORIZADO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    fecha_aprob = models.DateTimeField(db_column='FECHA_APROB', blank=True, null=True)  # Field name made lowercase.
    user_aprob = models.IntegerField(db_column='USER_APROB', blank=True, null=True)  # Field name made lowercase.

    class Meta:
        managed = False
        db_table = 'CAB_PEDIDO'


class DetPedido(models.Model):
    id_detalle = models.AutoField(db_column='ID_DETALLE', primary_key=True)  # Field name made lowercase.
    id_doc = models.DecimalField(db_column='ID_DOC', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    no_linea = models.DecimalField(db_column='No_LINEA', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    clase_doc_base = models.CharField(db_column='CLASE_DOC_BASE', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ref_doc_base = models.DecimalField(db_column='REF_DOC_BASE', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    estatus_linea = models.CharField(db_column='ESTATUS_LINEA', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    clase_art = models.CharField(db_column='CLASE_ART', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_articulo = models.CharField(db_column='ID_ARTICULO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    descrip_art = models.CharField(db_column='DESCRIP_ART', max_length=500, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cantidad = models.DecimalField(db_column='CANTIDAD', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cant_ent = models.DecimalField(db_column='CANT_ENT', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cant_pend = models.DecimalField(db_column='CANT_PEND', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    marca = models.CharField(db_column='MARCA', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    modelo = models.CharField(db_column='MODELO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    color = models.CharField(db_column='COLOR', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    chasis = models.CharField(db_column='CHASIS', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    maquina = models.CharField(db_column='MAQUINA', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ano = models.DecimalField(db_column='ANO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    medida = models.CharField(db_column='MEDIDA', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    lote = models.CharField(db_column='LOTE', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    no_lote = models.CharField(db_column='No_LOTE', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    venc_lote = models.DateTimeField(db_column='VENC_LOTE', blank=True, null=True)  # Field name made lowercase.
    costo = models.DecimalField(db_column='COSTO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    precio = models.DecimalField(db_column='PRECIO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    precio_bruto = models.DecimalField(db_column='PRECIO_BRUTO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    porc_desc = models.DecimalField(db_column='PORC_DESC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_impto = models.DecimalField(db_column='ID_IMPTO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    tarifa = models.DecimalField(db_column='TARIFA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    itbis = models.DecimalField(db_column='ITBIS', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_itbis = models.DecimalField(db_column='TOTAL_ITBIS', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_desc = models.DecimalField(db_column='TOTAL_DESC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_costo = models.DecimalField(db_column='TOTAL_COSTO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_precio = models.DecimalField(db_column='TOTAL_PRECIO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_precio_neto = models.DecimalField(db_column='TOTAL_PRECIO_NETO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    total_linea = models.DecimalField(db_column='TOTAL_LINEA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_almacen = models.DecimalField(db_column='ID_ALMACEN', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    id_vendedor = models.DecimalField(db_column='ID_VENDEDOR', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    porc_com = models.DecimalField(db_column='PORC_COM', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cta_ingreso = models.CharField(db_column='CTA_INGRESO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_gastos = models.CharField(db_column='CTA_GASTOS', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_costos = models.CharField(db_column='CTA_COSTOS', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_inv = models.CharField(db_column='CTA_INV', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_impto = models.CharField(db_column='CTA_IMPTO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_dev_venta = models.CharField(db_column='CTA_DEV_VENTA', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    precio_tras_desc = models.DecimalField(db_column='PRECIO_TRAS_DESC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    fecha_cont = models.DateTimeField(db_column='FECHA_CONT', blank=True, null=True)  # Field name made lowercase.
    id_cliente = models.DecimalField(db_column='ID_CLIENTE', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    cebe = models.CharField(db_column='CEBE', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ceco = models.CharField(db_column='CECO', max_length=12, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    periodo_cont = models.CharField(db_column='PERIODO_CONT', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ejercicio = models.DecimalField(db_column='EJERCICIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    ingreso_bruto = models.DecimalField(db_column='INGRESO_BRUTO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    referencia = models.CharField(db_column='REFERENCIA', max_length=15, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    observacion = models.TextField(db_column='OBSERVACION', db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    largo = models.DecimalField(db_column='LARGO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ancho = models.DecimalField(db_column='ANCHO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cant_und = models.DecimalField(db_column='CANT_UND', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    no_linea_base = models.IntegerField(db_column='No_LINEA_BASE', blank=True, null=True)  # Field name made lowercase.

    class Meta:
        managed = False
        db_table = 'DET_PEDIDO'


class MaestroSn(models.Model):
    id_serie = models.IntegerField(db_column='ID_SERIE', blank=True, null=True)  # Field name made lowercase.
    id_sn = models.CharField(db_column='ID_SN', primary_key=True, max_length=12, db_collation='SQL_Latin1_General_CP1_CI_AS')  # Field name made lowercase.
    nom_socio = models.CharField(db_column='NOM_SOCIO', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    contacto = models.CharField(db_column='CONTACTO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    clase_sn = models.CharField(db_column='CLASE_SN', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_grupo = models.DecimalField(db_column='ID_GRUPO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    descripcion = models.CharField(db_column='DESCRIPCION', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    secuencia = models.CharField(db_column='SECUENCIA', max_length=5, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_asociada = models.CharField(db_column='CTA_ASOCIADA', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_anticipo = models.CharField(db_column='CTA_ANTICIPO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_comp_ant = models.CharField(db_column='CTA_COMP_ANT', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo_sn = models.CharField(db_column='TIPO_SN', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    rnc_ced = models.CharField(db_column='RNC_CED', max_length=13, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    dir_factura = models.CharField(db_column='DIR_FACTURA', max_length=200, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    dir_mercancia = models.CharField(db_column='DIR_MERCANCIA', max_length=200, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tel1 = models.CharField(db_column='TEL1', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tel2 = models.CharField(db_column='TEL2', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    fax = models.CharField(db_column='FAX', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    email = models.CharField(db_column='EMAIL', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cobro_elect = models.CharField(db_column='COBRO_ELECT', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cuenta_destino = models.CharField(db_column='CUENTA_DESTINO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo_cuenta = models.CharField(db_column='TIPO_CUENTA', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    mon_destino = models.CharField(db_column='MON_DESTINO', max_length=3, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    banco_destino = models.CharField(db_column='BANCO_DESTINO', max_length=8, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    digi_banco_destino = models.CharField(db_column='DIGI_BANCO_DESTINO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cod_operacion = models.CharField(db_column='COD_OPERACION', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_sector = models.DecimalField(db_column='ID_SECTOR', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    comentario = models.CharField(db_column='COMENTARIO', max_length=200, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    saldo = models.DecimalField(db_column='SALDO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    saldo_ant = models.DecimalField(db_column='SALDO_ANT', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    mora = models.DecimalField(db_column='MORA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_condicion = models.DecimalField(db_column='ID_CONDICION', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    dia = models.DecimalField(db_column='DIA', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    condicion = models.CharField(db_column='CONDICION', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    lim_credito = models.DecimalField(db_column='LIM_CREDITO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    tarifa_int = models.DecimalField(db_column='TARIFA_INT', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_precio = models.DecimalField(db_column='ID_PRECIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    factor = models.DecimalField(db_column='FACTOR', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    id_vendedor = models.DecimalField(db_column='ID_VENDEDOR', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    nom_vend = models.CharField(db_column='NOM_VEND', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    moneda = models.CharField(db_column='MONEDA', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    celular = models.CharField(db_column='CELULAR', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    imagen = models.BinaryField(db_column='IMAGEN', blank=True, null=True)  # Field name made lowercase.
    foto = models.CharField(db_column='FOTO', max_length=255, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    fecha_creacion = models.DateTimeField(db_column='FECHA_CREACION', blank=True, null=True)  # Field name made lowercase.
    fecha_act = models.DateTimeField(db_column='FECHA_ACT', blank=True, null=True)  # Field name made lowercase.
    id_usuario = models.DecimalField(db_column='ID_USUARIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    bloqueado = models.CharField(db_column='BLOQUEADO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_ncf = models.DecimalField(db_column='ID_NCF', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    tipo_ncf = models.CharField(db_column='TIPO_NCF', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo_gasto = models.CharField(db_column='TIPO_GASTO', max_length=8, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    retencion = models.CharField(db_column='RETENCION', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    movimiento = models.CharField(db_column='MOVIMIENTO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nomref1 = models.CharField(db_column='NOMREF1', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    telref1 = models.CharField(db_column='TELREF1', max_length=13, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    parentref1 = models.CharField(db_column='PARENTREF1', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nomref2 = models.CharField(db_column='NOMREF2', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    telref2 = models.CharField(db_column='TELREF2', max_length=13, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    parentref2 = models.CharField(db_column='PARENTREF2', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nomref3 = models.CharField(db_column='NOMREF3', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    telref3 = models.CharField(db_column='TELREF3', max_length=13, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    parentref3 = models.CharField(db_column='PARENTREF3', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nomref4 = models.CharField(db_column='NOMREF4', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    telref4 = models.CharField(db_column='TELREF4', max_length=13, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    parentref4 = models.CharField(db_column='PARENTREF4', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    terminal = models.CharField(db_column='TERMINAL', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo_mov = models.CharField(db_column='TIPO_MOV', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    digi_chequeo = models.CharField(db_column='DIGI_CHEQUEO', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cod_ruta = models.CharField(db_column='COD_RUTA', max_length=8, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ret2 = models.CharField(db_column='RET2', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    retit = models.CharField(db_column='RETIT', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    credito = models.CharField(db_column='CREDITO', max_length=15, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codsap = models.CharField(db_column='CODSAP', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codant = models.CharField(db_column='CODANT', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    estatus = models.CharField(db_column='ESTATUS', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.

    class Meta:
        managed = False
        db_table = 'MAESTRO_SN'


class MaestroArticulo(models.Model):
    id_codigo = models.IntegerField(db_column='ID_CODIGO', blank=True, null=True)  # Field name made lowercase.
    id_articulo = models.CharField(db_column='ID_ARTICULO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS')  # Field name made lowercase.
    descrip_art = models.CharField(db_column='DESCRIP_ART', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    serie = models.DecimalField(db_column='SERIE', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    id_grupo = models.DecimalField(db_column='ID_GRUPO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    descrip_grupo = models.CharField(db_column='DESCRIP_GRUPO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    secuencia = models.CharField(db_column='SECUENCIA', max_length=5, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cod_barra = models.CharField(db_column='COD_BARRA', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    referencia = models.CharField(db_column='REFERENCIA', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    origen = models.CharField(db_column='ORIGEN', max_length=30, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    art_compra = models.CharField(db_column='ART_COMPRA', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    art_venta = models.CharField(db_column='ART_VENTA', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    art_inv = models.CharField(db_column='ART_INV', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    lote = models.CharField(db_column='LOTE', max_length=2, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    reservado = models.DecimalField(db_column='RESERVADO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    largo = models.DecimalField(db_column='LARGO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ancho = models.DecimalField(db_column='ANCHO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    grosor = models.DecimalField(db_column='GROSOR', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    patron = models.DecimalField(db_column='PATRON', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    stock = models.DecimalField(db_column='STOCK', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    sol_cliente = models.DecimalField(db_column='SOL_CLIENTE', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    sol_prov = models.DecimalField(db_column='SOL_PROV', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cta_ingreso = models.CharField(db_column='CTA_INGRESO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_1 = models.CharField(db_column='NOM_1', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tipo_aprov = models.CharField(db_column='TIPO_APROV', max_length=15, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nivel_max = models.DecimalField(db_column='NIVEL_MAX', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    nivel_min = models.DecimalField(db_column='NIVEL_MIN', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    alm_dft = models.DecimalField(db_column='ALM_DFT', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    id_fabricante = models.DecimalField(db_column='ID_FABRICANTE', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    descrip_fab = models.CharField(db_column='DESCRIP_FAB', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    marca = models.CharField(db_column='MARCA', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    modelo = models.CharField(db_column='MODELO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ano = models.CharField(db_column='ANO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    porc_desc = models.DecimalField(db_column='PORC_DESC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    um_compra = models.CharField(db_column='UM_COMPRA', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cant_compra = models.DecimalField(db_column='CANT_COMPRA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ult_precio = models.DecimalField(db_column='ULT_PRECIO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    bloqueado = models.CharField(db_column='BLOQUEADO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    um_venta = models.CharField(db_column='UM_VENTA', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cant_venta = models.DecimalField(db_column='CANT_VENTA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    stock_inicial = models.DecimalField(db_column='STOCK_INICIAL', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_usuario = models.DecimalField(db_column='ID_USUARIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    imagen = models.BinaryField(db_column='IMAGEN', blank=True, null=True)  # Field name made lowercase.
    foto = models.CharField(db_column='FOTO', max_length=255, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    comentario = models.CharField(db_column='COMENTARIO', max_length=200, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    com_art = models.DecimalField(db_column='COM_ART', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    precio_det = models.DecimalField(db_column='PRECIO_DET', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    precio_usd = models.DecimalField(db_column='PRECIO_USD', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    precio_ya = models.DecimalField(db_column='PRECIO_YA', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ult_precio_oc = models.DecimalField(db_column='ULT_PRECIO_OC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ult_fecha_oc = models.DateTimeField(db_column='ULT_FECHA_OC', blank=True, null=True)  # Field name made lowercase.
    moneda = models.CharField(db_column='MONEDA', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ult_precio_vt = models.DecimalField(db_column='ULT_PRECIO_VT', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ult_fecha_vt = models.DateTimeField(db_column='ULT_FECHA_VT', blank=True, null=True)  # Field name made lowercase.
    fecha_creacion = models.DateTimeField(db_column='FECHA_CREACION', blank=True, null=True)  # Field name made lowercase.
    fecha_act = models.DateTimeField(db_column='FECHA_ACT', blank=True, null=True)  # Field name made lowercase.
    costo = models.DecimalField(db_column='COSTO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    costo_mo = models.DecimalField(db_column='COSTO_MO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_precio = models.DecimalField(db_column='ID_PRECIO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    descrip_precio = models.CharField(db_column='DESCRIP_PRECIO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    factor_precio = models.DecimalField(db_column='FACTOR_PRECIO', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    tipo_precio = models.CharField(db_column='TIPO_PRECIO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_gasto = models.CharField(db_column='CTA_GASTO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_2 = models.CharField(db_column='NOM_2', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_costo = models.CharField(db_column='CTA_COSTO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_3 = models.CharField(db_column='NOM_3', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    clase_art = models.CharField(db_column='CLASE_ART', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    um_inv = models.CharField(db_column='UM_INV', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    valor_stock = models.DecimalField(db_column='VALOR_STOCK', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_impto_vt = models.DecimalField(db_column='ID_IMPTO_VT', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    cod_impto_vt = models.CharField(db_column='COD_IMPTO_VT', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tarifa_vt = models.DecimalField(db_column='TARIFA_VT', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    id_impto_oc = models.DecimalField(db_column='ID_IMPTO_OC', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    cod_impto_oc = models.CharField(db_column='COD_IMPTO_OC', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tarifa_oc = models.DecimalField(db_column='TARIFA_OC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    cta_impto_vt = models.CharField(db_column='CTA_IMPTO_VT', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_4 = models.CharField(db_column='NOM_4', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_impto_oc = models.CharField(db_column='CTA_IMPTO_OC', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_5 = models.CharField(db_column='NOM_5', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_inv = models.CharField(db_column='CTA_INV', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_6 = models.CharField(db_column='NOM_6', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_dotacion = models.CharField(db_column='CTA_DOTACION', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_desviacion = models.CharField(db_column='CTA_DESVIACION', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_dev_venta = models.CharField(db_column='CTA_DEV_VENTA', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_dif_precio = models.CharField(db_column='CTA_DIF_PRECIO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_dif_inv = models.CharField(db_column='CTA_DIF_INV', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_red_stock = models.CharField(db_column='CTA_RED_STOCK', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_aum_stock = models.CharField(db_column='CTA_AUM_STOCK', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_comp_gasto = models.CharField(db_column='CTA_COMP_GASTO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_cr_venta = models.CharField(db_column='CTA_CR_VENTA', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_desv_wip = models.CharField(db_column='CTA_DESV_WIP', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cta_stock_proc = models.CharField(db_column='CTA_STOCK_PROC', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_tipo = models.DecimalField(db_column='ID_TIPO', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    tipo_art = models.CharField(db_column='TIPO_ART', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    factor = models.DecimalField(db_column='FACTOR', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    ubicacion1 = models.CharField(db_column='UBICACION1', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ubicacion2 = models.CharField(db_column='UBICACION2', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ubicacion3 = models.CharField(db_column='UBICACION3', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ubicacion4 = models.CharField(db_column='UBICACION4', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    movimiento = models.CharField(db_column='MOVIMIENTO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    terminal = models.CharField(db_column='TERMINAL', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    cadena = models.CharField(db_column='CADENA', max_length=500, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    uso = models.CharField(db_column='USO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    motor = models.CharField(db_column='MOTOR', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    lineamat = models.CharField(db_column='LINEAMAT', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    sublinea1 = models.CharField(db_column='SUBLINEA1', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codsap = models.CharField(db_column='CODSAP', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codant = models.CharField(db_column='CODANT', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    version = models.CharField(db_column='VERSION', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    combustible = models.CharField(db_column='COMBUSTIBLE', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    color = models.CharField(db_column='COLOR', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ceco = models.CharField(db_column='CECO', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    orden = models.IntegerField(db_column='ORDEN', blank=True, null=True)  # Field name made lowercase.
    invturno = models.CharField(db_column='INVTURNO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    insumo = models.CharField(db_column='INSUMO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    tela = models.CharField(db_column='TELA', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codprod = models.CharField(db_column='CODPROD', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codfami = models.CharField(db_column='CODFAMI', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codcomp = models.CharField(db_column='CODCOMP', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codcara = models.CharField(db_column='CODCARA', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codtama = models.CharField(db_column='CODTAMA', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    coddise = models.CharField(db_column='CODDISE', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    codcolo = models.CharField(db_column='CODCOLO', max_length=10, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_sn = models.CharField(db_column='ID_SN', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nom_socio = models.CharField(db_column='NOM_SOCIO', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    idsubgrupo = models.IntegerField(db_column='IDSUBGRUPO', blank=True, null=True)  # Field name made lowercase.
    nomsubgrupo = models.CharField(db_column='NOMSUBGRUPO', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    idcategoria = models.IntegerField(db_column='IDCATEGORIA', blank=True, null=True)  # Field name made lowercase.
    nomcategoria = models.CharField(db_column='NOMCATEGORIA', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.

    class Meta:
        managed = False
        db_table = 'MAESTRO_ARTICULO'


class Usuario(models.Model):
    id_usuario = models.AutoField(db_column='ID_USUARIO', primary_key=True)  # Field name made lowercase.
    usuario = models.CharField(db_column='USUARIO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS')  # Field name made lowercase.
    clave = models.CharField(db_column='CLAVE', max_length=12, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    clave_nueva = models.CharField(db_column='CLAVE_NUEVA', max_length=12, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nombre = models.CharField(db_column='NOMBRE', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    ceco = models.CharField(db_column='CECO', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    depto = models.CharField(db_column='DEPTO', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    nivel = models.CharField(db_column='NIVEL', max_length=100, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    porc_desc = models.DecimalField(db_column='PORC_DESC', max_digits=19, decimal_places=4, blank=True, null=True)  # Field name made lowercase.
    estado = models.CharField(db_column='ESTADO', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    conectado = models.CharField(db_column='CONECTADO', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_empresa = models.DecimalField(db_column='ID_EMPRESA', max_digits=18, decimal_places=0, blank=True, null=True)  # Field name made lowercase.
    cambiar_clave = models.CharField(db_column='CAMBIAR_CLAVE', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_caja = models.IntegerField(db_column='ID_CAJA', blank=True, null=True)  # Field name made lowercase.
    pos = models.CharField(db_column='POS', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    terminal = models.CharField(db_column='TERMINAL', max_length=50, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    version = models.CharField(db_column='VERSION', max_length=20, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    preliminar = models.CharField(db_column='PRELIMINAR', max_length=1, db_collation='SQL_Latin1_General_CP1_CI_AS', blank=True, null=True)  # Field name made lowercase.
    id_firma = models.IntegerField(db_column='ID_FIRMA', blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'USUARIO'
