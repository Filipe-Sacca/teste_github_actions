#!/usr/bin/env python3
"""
Issue Analyzer Agent
Analisa issues do GitHub usando Gemini e envia resumo para Slack.
"""

import os
import json
import requests
import google.generativeai as genai
from tavily import TavilyClient

# =============================================================================
# CONFIGURACAO
# =============================================================================

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

# Dados da issue (vindos do GitHub Actions)
ISSUE_TITLE = os.environ.get("ISSUE_TITLE", "")
ISSUE_BODY = os.environ.get("ISSUE_BODY", "")
ISSUE_NUMBER = os.environ.get("ISSUE_NUMBER", "")
ISSUE_URL = os.environ.get("ISSUE_URL", "")
ISSUE_AUTHOR = os.environ.get("ISSUE_AUTHOR", "")
ASSIGNEE_USERNAME = os.environ.get("ASSIGNEE_USERNAME", "")
REPO_NAME = os.environ.get("REPO_NAME", "")

# Mapeamento GitHub username -> Slack user ID (para menções)
# Edite este dicionário com seus usuários
USER_MAPPING = {
    # "github-username": "SLACK_USER_ID",
    # Exemplo: "filipe": "U01ABC123XYZ",
}

# =============================================================================
# FUNCOES
# =============================================================================

def web_search(query: str) -> str:
    """Realiza pesquisa na web usando Tavily."""
    if not TAVILY_API_KEY:
        return "Web search não disponível (TAVILY_API_KEY não configurada)"

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        response = client.search(query=query, max_results=3)

        results = []
        for r in response.get("results", []):
            results.append(f"- {r['title']}: {r['content'][:200]}...")

        return "\n".join(results) if results else "Nenhum resultado encontrado"
    except Exception as e:
        return f"Erro na pesquisa: {str(e)}"


def analyze_with_gemini(issue_title: str, issue_body: str) -> dict:
    """
    Usa Gemini para analisar a issue.
    Retorna dict com: summary, suggestions, needs_research, search_results
    """
    genai.configure(api_key=GOOGLE_API_KEY)

    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash-exp",
        generation_config={
            "temperature": 0.7,
            "max_output_tokens": 1024,
        }
    )

    # Prompt para análise inicial
    analysis_prompt = f"""Você é um assistente técnico que analisa issues de repositórios GitHub.

Analise a seguinte issue e forneça:
1. Um resumo claro e conciso (2-3 frases)
2. Classificação: bug | feature | question | documentation | other
3. Prioridade sugerida: low | medium | high | critical
4. 2-3 sugestões práticas para resolver/implementar
5. Decida: precisa de pesquisa web para informações atualizadas? (sim/não)
   - Responda "sim" apenas se a issue mencionar versões específicas, erros obscuros,
     bibliotecas externas recentes, ou problemas que requerem contexto atualizado.

Issue:
Título: {issue_title}
Corpo: {issue_body if issue_body else "(sem descrição)"}

Responda em JSON válido com esta estrutura:
{{
    "summary": "resumo aqui",
    "classification": "bug|feature|question|documentation|other",
    "priority": "low|medium|high|critical",
    "suggestions": ["sugestão 1", "sugestão 2", "sugestão 3"],
    "needs_research": true/false,
    "research_query": "query para pesquisar se needs_research for true, senão null"
}}
"""

    response = model.generate_content(analysis_prompt)
    response_text = response.text.strip()

    # Limpar markdown se presente
    if response_text.startswith("```"):
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]
    response_text = response_text.strip()

    try:
        analysis = json.loads(response_text)
    except json.JSONDecodeError:
        # Fallback se o JSON falhar
        analysis = {
            "summary": response_text[:500],
            "classification": "other",
            "priority": "medium",
            "suggestions": ["Revisar a issue manualmente"],
            "needs_research": False,
            "research_query": None
        }

    # Se precisa de pesquisa, faz a busca
    if analysis.get("needs_research") and analysis.get("research_query"):
        search_results = web_search(analysis["research_query"])
        analysis["search_results"] = search_results

        # Segunda chamada para enriquecer sugestões com resultados da pesquisa
        enrichment_prompt = f"""Com base na pesquisa web abaixo, adicione informações relevantes às sugestões.

Issue: {issue_title}
Pesquisa realizada: {analysis['research_query']}
Resultados:
{search_results}

Sugestões originais: {json.dumps(analysis['suggestions'])}

Retorne apenas um JSON com as sugestões atualizadas (máximo 3):
{{"suggestions": ["sugestão 1 melhorada", "sugestão 2 melhorada", "sugestão 3 melhorada"]}}
"""
        enrichment_response = model.generate_content(enrichment_prompt)
        try:
            enrichment_text = enrichment_response.text.strip()
            if enrichment_text.startswith("```"):
                enrichment_text = enrichment_text.split("```")[1]
                if enrichment_text.startswith("json"):
                    enrichment_text = enrichment_text[4:]
            enriched = json.loads(enrichment_text.strip())
            analysis["suggestions"] = enriched.get("suggestions", analysis["suggestions"])
        except:
            pass  # Mantém sugestões originais se falhar

    return analysis


def send_to_slack(analysis: dict) -> bool:
    """Envia o resultado da análise para o Slack."""
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL não configurada - pulando envio")
        return False

    # Emojis por classificação
    class_emoji = {
        "bug": ":bug:",
        "feature": ":sparkles:",
        "question": ":question:",
        "documentation": ":books:",
        "other": ":memo:"
    }

    # Emojis por prioridade
    priority_emoji = {
        "low": ":white_circle:",
        "medium": ":large_yellow_circle:",
        "high": ":large_orange_circle:",
        "critical": ":red_circle:"
    }

    classification = analysis.get("classification", "other")
    priority = analysis.get("priority", "medium")

    # Menção do usuário se mapeado
    slack_user_id = USER_MAPPING.get(ASSIGNEE_USERNAME)
    mention = f"<@{slack_user_id}>" if slack_user_id else f"@{ASSIGNEE_USERNAME}"

    # Formatar sugestões
    suggestions_text = "\n".join([f"• {s}" for s in analysis.get("suggestions", [])])

    # Construir mensagem
    slack_message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📋 Nova Issue #{ISSUE_NUMBER}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Repositório:*\n{REPO_NAME}"},
                    {"type": "mrkdwn", "text": f"*Atribuída para:*\n{mention}"}
                ]
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tipo:*\n{class_emoji.get(classification, ':memo:')} {classification}"},
                    {"type": "mrkdwn", "text": f"*Prioridade:*\n{priority_emoji.get(priority, ':white_circle:')} {priority}"}
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Título:*\n<{ISSUE_URL}|{ISSUE_TITLE}>"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📝 Resumo da IA:*\n{analysis.get('summary', 'N/A')}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*💡 Sugestões:*\n{suggestions_text}"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Criada por: {ISSUE_AUTHOR} | Análise por: Gemini 2.0 Flash"
                    }
                ]
            }
        ]
    }

    # Se teve pesquisa web, adicionar nota
    if analysis.get("search_results"):
        slack_message["blocks"].insert(-1, {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🔍 _Pesquisa web realizada para complementar a análise_"
                }
            ]
        })

    # Enviar para Slack
    try:
        response = requests.post(
            SLACK_WEBHOOK_URL,
            json=slack_message,
            headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        print(f"Mensagem enviada para Slack com sucesso!")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Erro ao enviar para Slack: {e}")
        return False


# =============================================================================
# MAIN
# =============================================================================

def main():
    print(f"🔍 Analisando issue #{ISSUE_NUMBER}: {ISSUE_TITLE}")
    print(f"👤 Atribuída para: {ASSIGNEE_USERNAME}")

    if not GOOGLE_API_KEY:
        print("❌ GOOGLE_API_KEY não configurada!")
        exit(1)

    # Analisar com Gemini
    print("🤖 Enviando para análise do Gemini...")
    analysis = analyze_with_gemini(ISSUE_TITLE, ISSUE_BODY)

    print(f"📊 Classificação: {analysis.get('classification')}")
    print(f"🎯 Prioridade: {analysis.get('priority')}")
    print(f"📝 Resumo: {analysis.get('summary')}")

    if analysis.get("needs_research"):
        print(f"🔍 Pesquisa web realizada: {analysis.get('research_query')}")

    # Enviar para Slack
    print("📤 Enviando para Slack...")
    send_to_slack(analysis)

    print("✅ Análise concluída!")


if __name__ == "__main__":
    main()
