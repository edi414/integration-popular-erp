import xml.etree.ElementTree as ET
import random
import string
from datetime import datetime
from typing import Dict, List, Optional
import logging

class NFeParser:
    """
    Handler especializado em realizar o parsing de XMLs de NF-e (v4.00)
    Garantindo 100% de paridade com a lógica do process_xml_files.py.
    """
    
    NAMESPACE = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}
    
    @staticmethod
    def _generate_random_id() -> int:
        """Gera um ID numérico aleatório de 9 dígitos para evitar colisões."""
        return random.randint(100000000, 999999999)

    @staticmethod
    def _get_text(element: ET.Element, path: str, namespaces: Dict = None) -> Optional[str]:
        """Helper para extrair texto de um elemento com segurança."""
        if element is None:
            return None
        found = element.find(path, namespaces or NFeParser.NAMESPACE)
        return found.text if found is not None else None

    def parse_summary(self, xml_content: str) -> Dict:
        """
        Extrai informações de cabeçalho da NFe para a tabela nfes_informations.
        """
        root = ET.fromstring(xml_content)
        infNFe = root.find('.//nfe:infNFe', self.NAMESPACE)
        chave = infNFe.attrib.get('Id', '').replace('NFe', '') if infNFe is not None else None
        
        emit = root.find('.//nfe:emit', self.NAMESPACE)
        cnpj_fornecedor = self._get_text(emit, 'nfe:CNPJ') or self._get_text(emit, 'nfe:CPF')
        nome_fornecedor = self._get_text(emit, 'nfe:xNome')
        fantasia_fornecedor = self._get_text(emit, 'nfe:xFant') or ""
        cep_fornecedor = self._get_text(emit, './/nfe:CEP')
        
        dest = root.find('.//nfe:dest', self.NAMESPACE)
        cnpj_cliente = self._get_text(dest, 'nfe:CNPJ')
        
        fat = root.find('.//nfe:cobr/nfe:fat', self.NAMESPACE)
        v_orig = self._get_text(fat, 'nfe:vOrig')
        v_desc = self._get_text(fat, 'nfe:vDesc')
        v_liq = self._get_text(fat, 'nfe:vLiq')
        
        if v_orig is None:
            total = root.find('.//nfe:total/nfe:ICMSTot', self.NAMESPACE)
            v_orig = self._get_text(total, 'nfe:vNF')
        
        pag = root.find('.//nfe:pag/nfe:detPag', self.NAMESPACE)
        v_pag = self._get_text(pag, 'nfe:vPag')
        
        dup = root.find('.//nfe:cobr/nfe:dup', self.NAMESPACE)
        d_venc = self._get_text(dup, 'nfe:dVenc')
        v_dup = self._get_text(dup, 'nfe:vDup')

        # Retornando chaves em MINÚSCULO para coincidir com o schema do Postgres
        return {
            'id': self._generate_random_id(),
            'data_registro': datetime.now().strftime('%Y-%m-%d'),
            'chave_nfe': chave,
            'cpnj_fornecedor': cnpj_fornecedor,
            'nome_fornecedor': nome_fornecedor,
            'nome_fantasia_fornecedor': fantasia_fornecedor,
            'cep_fornecedor': cep_fornecedor,
            'cnpj_cliente': cnpj_cliente,
            'valor_total': v_orig,
            'valor_desconto': v_desc,
            'valor_liquido': v_liq,
            'valor_pagamento': v_pag,
            'data_vencimento_dup': d_venc,
            'valor_duplicada': v_dup
        }

    def parse_items(self, xml_content: str) -> List[Dict]:
        """
        Extrai itens da NFe para a tabela precificacao.
        """
        root = ET.fromstring(xml_content)
        infNFe = root.find('.//nfe:infNFe', self.NAMESPACE)
        chave = infNFe.attrib.get('Id', '').replace('NFe', '') if infNFe is not None else None
        
        items = []
        for det in root.findall('.//nfe:det', self.NAMESPACE):
            prod = det.find('nfe:prod', self.NAMESPACE)
            
            item = {
                'chave_nfe': chave,
                'descricao': self._get_text(prod, 'nfe:xProd'),
                'ean_trib': self._get_text(prod, 'nfe:cEANTrib'),
                'sale_type': self._get_text(prod, 'nfe:uCom'),
                'quantidade': float(self._get_text(prod, 'nfe:qCom') or 0),
                'valor_total': float(self._get_text(prod, 'nfe:vProd') or 0),
                'valor_desconto': float(self._get_text(prod, 'nfe:vDesc') or 0),
                'quantidade_tributada': self._get_text(prod, 'nfe:qTrib'),
                'unidade_tributada': self._get_text(prod, 'nfe:uTrib'),
                'ean': self._get_text(prod, 'nfe:cEAN'),
                'ncm': self._get_text(prod, 'nfe:NCM'),
                'cest': self._get_text(prod, 'nfe:CEST'),
                'cfop': self._get_text(prod, 'nfe:CFOP'),
                'valor_un_trib': self._get_text(prod, 'nfe:vUnTrib'),
            }
            
            imposto = det.find('nfe:imposto', self.NAMESPACE)
            icms_container = imposto.find('nfe:ICMS', self.NAMESPACE)
            if icms_container is not None:
                for icms_xx in icms_container:
                    for field in icms_xx:
                        tag = field.tag.split('}')[1] if '}' in field.tag else field.tag
                        if tag == 'CST' or tag == 'CSOSN':
                            item['cst_icms'] = field.text
                        elif tag == 'vBC':
                            item['base_calculo_icms'] = float(field.text or 0)
                        elif tag == 'pICMS':
                            item['aliq_icms'] = float(field.text or 0)
                        elif tag == 'vICMS':
                            item['valor_icms'] = float(field.text or 0)
                        elif tag == 'vBCST':
                            item['vb_icms_st'] = float(field.text or 0)
                        elif tag == 'pICMSST':
                            item['icms_st'] = float(field.text or 0)
                        elif tag == 'vICMSST':
                            item['v_icms_st'] = float(field.text or 0)
            
            ipi_trib = imposto.find('.//nfe:IPI/nfe:IPITrib', self.NAMESPACE)
            if ipi_trib is not None:
                item['cst_ipi'] = self._get_text(ipi_trib, 'nfe:CST')
                item['aliq_ipi'] = float(self._get_text(ipi_trib, 'nfe:pIPI') or 0)
                item['valor_ipi'] = float(self._get_text(ipi_trib, 'nfe:vIPI') or 0)
            
            pis_aliq = imposto.find('.//nfe:PIS/nfe:PISAliq', self.NAMESPACE) or imposto.find('.//nfe:PIS/nfe:PISOutr', self.NAMESPACE)
            if pis_aliq is not None:
                item['cst_pis'] = self._get_text(pis_aliq, 'nfe:CST')
                item['base_calculo_pis'] = float(self._get_text(pis_aliq, 'nfe:vBC') or 0)
                item['aliq_pis'] = float(self._get_text(pis_aliq, 'nfe:pPIS') or 0)
                item['valor_pis'] = float(self._get_text(pis_aliq, 'nfe:vPIS') or 0)
                
            cofins_aliq = imposto.find('.//nfe:COFINS/nfe:COFINSAliq', self.NAMESPACE) or imposto.find('.//nfe:COFINS/nfe:COFINSOutr', self.NAMESPACE)
            if cofins_aliq is not None:
                item['cst_cofins'] = self._get_text(cofins_aliq, 'nfe:CST')
                item['base_calculo_cofins'] = float(self._get_text(cofins_aliq, 'nfe:vBC') or 0)
                item['aliq_cofins'] = float(self._get_text(cofins_aliq, 'nfe:pCOFINS') or 0)
                item['valor_cofins'] = float(self._get_text(cofins_aliq, 'nfe:vCOFINS') or 0)

            items.append(item)
            
        return items
