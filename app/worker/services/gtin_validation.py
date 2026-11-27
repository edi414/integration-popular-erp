from stdnum import ean


def validar_gtin_novo(gtin_novo) -> str | None:
    """
    Regra R3 (docs/g3_troca_gtin.md): o código novo precisa ser um EAN/GTIN válido
    (8, 12, 13 ou 14 dígitos, só números, dígito verificador correto).

    Retorna o motivo da rejeição, ou None se o código for válido.
    """
    if not isinstance(gtin_novo, str) or not gtin_novo.strip():
        return "Código novo ausente ou vazio"
    if not ean.is_valid(gtin_novo.strip()):
        return f"Código novo {gtin_novo!r} não é um EAN/GTIN válido"
    return None
