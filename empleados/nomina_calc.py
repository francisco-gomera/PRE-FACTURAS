"""
Motor de cálculo de nómina — República Dominicana
Basado en Código de Trabajo (Ley 16-92), TSS y DGII (2026).

Todas las deducciones legales son opcionales y controladas por los
toggles del período (aplicar_afp, aplicar_sfs, aplicar_srl, aplicar_isr).
"""

from decimal import Decimal, ROUND_HALF_UP

# ── Constantes legales 2026 ──────────────────────────────────────────────

AFP_EMPLEADO = Decimal("0.0287")
AFP_EMPLEADOR = Decimal("0.0710")
AFP_TOPE_MENSUAL = Decimal("464460.00")

SFS_EMPLEADO = Decimal("0.0304")
SFS_EMPLEADOR = Decimal("0.0709")
SFS_TOPE_MENSUAL = Decimal("232230.00")

SRL_EMPLEADOR = Decimal("0.0110")
SRL_TOPE_MENSUAL = Decimal("92892.00")

DIVISOR_DIAS = Decimal("23.83")
HORAS_DIA = Decimal("8")

# ISR Anual 2026 — (límite superior, tasa marginal, base excedente, cuota fija)
ISR_BRACKETS = [
    (Decimal("416220.00"), Decimal("0"), Decimal("0"), Decimal("0")),
    (Decimal("624329.00"), Decimal("0.15"), Decimal("416220.01"), Decimal("0")),
    (Decimal("867123.00"), Decimal("0.20"), Decimal("624329.01"), Decimal("31216")),
    (None, Decimal("0.25"), Decimal("867123.01"), Decimal("79776")),
]

TWO = Decimal("0.01")


def _r(value):
    """Round to 2 decimal places."""
    return value.quantize(TWO, rounding=ROUND_HALF_UP)


# ── TSS ──────────────────────────────────────────────────────────────────

def calcular_afp(salario_mensual):
    """Calcula AFP empleado y empleador."""
    base = min(salario_mensual, AFP_TOPE_MENSUAL)
    return _r(base * AFP_EMPLEADO), _r(base * AFP_EMPLEADOR)


def calcular_sfs(salario_mensual):
    """Calcula SFS empleado y empleador."""
    base = min(salario_mensual, SFS_TOPE_MENSUAL)
    return _r(base * SFS_EMPLEADO), _r(base * SFS_EMPLEADOR)


def calcular_srl(salario_mensual):
    """Calcula SRL empleador."""
    base = min(salario_mensual, SRL_TOPE_MENSUAL)
    return _r(base * SRL_EMPLEADOR)


# ── ISR ──────────────────────────────────────────────────────────────────

def calcular_isr_mensual(salario_bruto_mensual, tss_empleado_mensual):
    """
    Calcula ISR mensual retenido.
    1. Resta la TSS del empleado al bruto para obtener la renta gravable mensual.
    2. Anualiza multiplicando por 12.
    3. Aplica la tabla progresiva del ISR.
    4. Divide entre 12 para obtener la retención mensual.
    """
    renta_mensual = salario_bruto_mensual - tss_empleado_mensual
    if renta_mensual <= 0:
        return Decimal("0")
    renta_anual = renta_mensual * 12

    isr_anual = Decimal("0")
    for limite, tasa, base_excedente, cuota_fija in ISR_BRACKETS:
        if limite is None or renta_anual <= limite:
            excedente = renta_anual - base_excedente
            if excedente > 0:
                isr_anual = cuota_fija + _r(excedente * tasa)
            break

    return _r(isr_anual / 12)


# ── Horas extras ─────────────────────────────────────────────────────────

def calcular_hora_normal(salario_mensual):
    """Valor de una hora normal de trabajo."""
    return _r(salario_mensual / DIVISOR_DIAS / HORAS_DIA)


def calcular_horas_extras(salario_mensual, horas_35, horas_100):
    """Calcula montos de horas extras al 35% y al 100%."""
    hora = calcular_hora_normal(salario_mensual)
    monto_35 = _r(hora * Decimal("1.35") * Decimal(str(horas_35)))
    monto_100 = _r(hora * Decimal("2.00") * Decimal(str(horas_100)))
    return monto_35, monto_100


# ── Regalía Pascual ──────────────────────────────────────────────────────

def calcular_regalia(salario_mensual, meses_trabajados_en_ano):
    """
    Regalía Pascual = 1/12 del salario ordinario acumulado en el año.
    Si trabajó todo el año: salario_mensual * 12 / 12 = salario_mensual.
    Si trabajó parcialmente: salario_mensual * meses / 12.
    """
    meses = min(Decimal(str(meses_trabajados_en_ano)), Decimal("12"))
    return _r(salario_mensual * meses / Decimal("12"))


# ── Vacaciones pagadas ───────────────────────────────────────────────────

def calcular_vacaciones_pagadas(salario_mensual, dias):
    """Pago de vacaciones = salario diario × días."""
    salario_diario = _r(salario_mensual / DIVISOR_DIAS)
    return _r(salario_diario * Decimal(str(dias)))


# ── Salario por período ─────────────────────────────────────────────────

def salario_por_periodo(salario_mensual, tipo_periodo):
    """Calcula el salario correspondiente al tipo de período."""
    if tipo_periodo == "SEMANAL":
        return _r(salario_mensual / Decimal("4.33"))
    elif tipo_periodo == "QUINCENAL":
        return _r(salario_mensual / Decimal("2"))
    return salario_mensual  # MENSUAL


def dias_por_periodo(tipo_periodo):
    """Días laborables estándar por tipo de período."""
    if tipo_periodo == "SEMANAL":
        return Decimal("5.5")
    elif tipo_periodo == "QUINCENAL":
        return Decimal("11.92")
    return DIVISOR_DIAS


# ── Generación de entrada ───────────────────────────────────────────────

def generar_entrada(empleado, periodo, extras=None):
    """
    Genera un diccionario con todos los campos calculados para una
    NominaEntrada. Los campos de extras permiten override manual
    (horas_extras_35, horas_extras_100, bonificacion, comisiones, etc.).

    Los cálculos de deducciones legales respetan los toggles del período.
    """
    extras = extras or {}
    salario_mensual = empleado.salario_base or Decimal("0")
    sal_periodo = salario_por_periodo(salario_mensual, periodo.tipo)
    dias = dias_por_periodo(periodo.tipo)

    horas_35 = Decimal(str(extras.get("horas_extras_35", 0)))
    horas_100 = Decimal(str(extras.get("horas_extras_100", 0)))
    monto_he35, monto_he100 = calcular_horas_extras(salario_mensual, horas_35, horas_100)

    bonificacion = Decimal(str(extras.get("bonificacion", 0)))
    comisiones = Decimal(str(extras.get("comisiones", 0)))
    vacaciones_pagadas = Decimal(str(extras.get("vacaciones_pagadas", 0)))
    regalia = Decimal(str(extras.get("regalia", 0)))
    otros_ingresos = Decimal(str(extras.get("otros_ingresos", 0)))

    total_ingresos = (
        sal_periodo + monto_he35 + monto_he100
        + bonificacion + comisiones + vacaciones_pagadas
        + regalia + otros_ingresos
    )

    # Deducciones legales — solo si el toggle está habilitado
    afp_emp = afp_pat = Decimal("0")
    sfs_emp = sfs_pat = Decimal("0")
    srl_pat = Decimal("0")
    isr = Decimal("0")

    # Para TSS usamos el salario mensual completo (no el del período)
    if periodo.aplicar_afp:
        afp_emp, afp_pat = calcular_afp(salario_mensual)
        # Ajustar al período
        if periodo.tipo == "QUINCENAL":
            afp_emp = _r(afp_emp / 2)
            afp_pat = _r(afp_pat / 2)
        elif periodo.tipo == "SEMANAL":
            afp_emp = _r(afp_emp / Decimal("4.33"))
            afp_pat = _r(afp_pat / Decimal("4.33"))

    if periodo.aplicar_sfs:
        sfs_emp, sfs_pat = calcular_sfs(salario_mensual)
        if periodo.tipo == "QUINCENAL":
            sfs_emp = _r(sfs_emp / 2)
            sfs_pat = _r(sfs_pat / 2)
        elif periodo.tipo == "SEMANAL":
            sfs_emp = _r(sfs_emp / Decimal("4.33"))
            sfs_pat = _r(sfs_pat / Decimal("4.33"))

    if periodo.aplicar_srl:
        srl_pat = calcular_srl(salario_mensual)
        if periodo.tipo == "QUINCENAL":
            srl_pat = _r(srl_pat / 2)
        elif periodo.tipo == "SEMANAL":
            srl_pat = _r(srl_pat / Decimal("4.33"))

    if periodo.aplicar_isr:
        tss_emp_mensual = Decimal("0")
        if periodo.aplicar_afp:
            tss_emp_mensual += calcular_afp(salario_mensual)[0]
        if periodo.aplicar_sfs:
            tss_emp_mensual += calcular_sfs(salario_mensual)[0]
        isr = calcular_isr_mensual(salario_mensual, tss_emp_mensual)
        if periodo.tipo == "QUINCENAL":
            isr = _r(isr / 2)
        elif periodo.tipo == "SEMANAL":
            isr = _r(isr / Decimal("4.33"))

    total_ded_legales = afp_emp + sfs_emp + isr

    adelanto = Decimal(str(extras.get("adelanto", 0)))
    prestamo = Decimal(str(extras.get("prestamo_descuento", 0)))
    otras_ded = Decimal(str(extras.get("otras_deducciones", 0)))
    total_otras_ded = adelanto + prestamo + otras_ded

    neto = total_ingresos - total_ded_legales - total_otras_ded

    return {
        "salario_periodo": sal_periodo,
        "dias_trabajados": dias,
        "horas_extras_35": horas_35,
        "monto_horas_extras_35": monto_he35,
        "horas_extras_100": horas_100,
        "monto_horas_extras_100": monto_he100,
        "bonificacion": bonificacion,
        "bonificacion_desc": str(extras.get("bonificacion_desc", "")),
        "comisiones": comisiones,
        "vacaciones_pagadas": vacaciones_pagadas,
        "regalia": regalia,
        "otros_ingresos": otros_ingresos,
        "otros_ingresos_desc": str(extras.get("otros_ingresos_desc", "")),
        "afp_empleado": afp_emp,
        "afp_empleador": afp_pat,
        "sfs_empleado": sfs_emp,
        "sfs_empleador": sfs_pat,
        "srl_empleador": srl_pat,
        "isr_retencion": isr,
        "adelanto": adelanto,
        "prestamo_descuento": prestamo,
        "otras_deducciones": otras_ded,
        "otras_deducciones_desc": str(extras.get("otras_deducciones_desc", "")),
        "total_ingresos": total_ingresos,
        "total_deducciones_legales": total_ded_legales,
        "total_otras_deducciones": total_otras_ded,
        "neto_pagar": neto,
        "notas": str(extras.get("notas", "")),
    }
