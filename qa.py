from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from sentence_transformers import SentenceTransformer
import numpy as np
import requests
import re
import argparse
import spacy
from spacy.lang.en import English

sentencer = English()
sentencer.add_pipe("sentencizer")

nlp = spacy.load("en_core_web_sm")

stopwords = set(open("stopwords.txt").read().splitlines())

embedding_model = SentenceTransformer(
    "flax-sentence-embeddings/all_datasets_v4_MiniLM-L6"
)


def get_proper_nouns(query):
    """Return proper nouns in a query

    >>> get_proper_nouns("Who is Joe Biden?")
    ['Joe Biden']

    >>> get_proper_nouns("The")
    []

    >>> get_proper_nouns("How many moons does Saturn have?")
    ['Saturn']
    """

    doc = nlp(query)
    return [e.text for e in doc.ents]


def get_words(query):
    """
    >>> sorted(get_words("What is the mass of Saturn?"))
    ['mass', 'saturn']
    """

    words = set(re.split(r"[\s\.\?\!]+", query.lower()))

    return words - stopwords


def search(query):
    """
    Uses the wbsearchentities action to return entities matching a description.

    >>> search("John S. Pistole")[0]['id']
    'Q1701660'
    """

    result = requests.get(
        f"https://www.wikidata.org/w/api.php?action=wbsearchentities&"
        f"search={query}&language=en&format=json"
    ).json()

    return result["search"]


def get_label(entity):
    """
    >>> get_label("http://www.wikidata.org/entity/Q613726")
    'yottagram'

    >>> get_label("Q613726")
    'yottagram'
    """

    entity = entity.split("/")[-1]

    result = requests.get(
        f"https://www.wikidata.org/w/api.php?action=wbgetentities&"
        f"ids={entity}&props=labels&languages=en&format=json"
    ).json()

    return result["entities"][entity]["labels"]["en"]["value"]


def get_prop_value(entity, prop):
    """
    >>> get_prop_value("Q193", "P2067")
    '568360 yottagram'
    """
    result = requests.get(
        f"https://www.wikidata.org/w/api.php?action=wbgetentities&"
        f"ids={entity}&props=claims&language=en&format=json"
    ).json()

    try:
        claim = result["entities"][entity]["claims"][prop][0]["mainsnak"]
    except KeyError:
        return None

    if "amount" in claim["datavalue"]["value"]:
        value = claim["datavalue"]["value"]["amount"].lstrip("+")
    elif "time" in claim["datavalue"]["value"]:
        value = claim["datavalue"]["value"]["time"]
    elif "id" in claim["datavalue"]["value"]:
        value = get_label(claim["datavalue"]["value"]["id"])
    else:
        value = claim["datavalue"]["value"]

    try:
        value += " " + get_label(claim["datavalue"]["value"]["unit"])
    except KeyError:
        pass

    return value


def search_prop(query):
    """
    Returns the property matching a query

    >>> search_prop("mass")['id']
    'P2067'
    >>> search_prop("color")['id']
    'P462'
    >>> search_prop("hair color")['id']
    'P1884'
    """
    result = requests.get(
        f"https://www.wikidata.org/w/api.php?action=wbsearchentities&"
        f"search={query}&type=property&language=en&format=json"
    ).json()

    return result["search"][0]


def get_summary(wikidata_id):
    """
    Return the Wikipedia summary for a given wikidata entity

    >>> 'Democratic Party' in get_summary("Q6279")
    True
    """
    data_result = requests.get(
        f"https://www.wikidata.org/w/api.php?action=wbgetentities&"
        f"props=sitelinks/urls&ids={wikidata_id}&format=json"
    ).json()
    en_title = data_result["entities"][wikidata_id]["sitelinks"]["enwiki"]["title"]

    wiki_result = requests.get(
        f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&"
        f"exintro&explaintext&redirects=1&titles={en_title}&format=json"
    ).json()

    summary = wiki_result["query"]["pages"].popitem()[1]["extract"]

    return summary


def generate(model, tokenizer, instruction, knowledge, dialog):
    if knowledge != "":
        knowledge = "[KNOWLEDGE] " + knowledge
    dialog = " EOS ".join(dialog)
    query = f"{instruction} [CONTEXT] {dialog} {knowledge}"
    input_ids = tokenizer(f"{query}", return_tensors="pt").input_ids
    outputs = model.generate(
        input_ids, max_length=128, min_length=8, top_p=0.9, do_sample=True
    )
    output = tokenizer.decode(outputs[0], skip_special_tokens=True)
    return output


def cos_sim(a, b):
    return np.matmul(a, np.transpose(b)) / (np.linalg.norm(a) * np.linalg.norm(b))


def get_topn_similar(anchor, inputs, n=1):
    """
    >>> get_topn_similar("What is Mars?", ["Mars is planet", "The sun is hot"])
    ['Mars is a planet']

    >>> get_topn_similar("Where is Paris?", ["Paris is rainy", "Paris is in France"])
    ['Paris is in France']
    """
    anchor_emb = embedding_model.encode(anchor)[None, :]
    inputs_emb = embedding_model.encode(inputs)

    similarities = list(zip(np.squeeze(cos_sim(anchor_emb, inputs_emb)), inputs))
    top_n = sorted(similarities, key=lambda s: -s[0])[:n]

    return [s[1] for s in top_n]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Rename files to a standard format")
    ap.add_argument(
        "--large",
        action="store_true",
        help="Use large model",
    )
    ap.add_argument(
        "--wikidata",
        action="store_true",
        help="Download knowledge from Wikidata",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Show verbose output",
    )
    args = ap.parse_args()

    if args.large:
        model_name = "microsoft/GODEL-v1_1-large-seq2seq"
    else:
        model_name = "microsoft/GODEL-v1_1-base-seq2seq"

    print(f"Loading {model_name}...")

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name, low_cpu_mem_usage=True)

    dialog = []

    while True:
        # Instruction for a chitchat task
        instruction = "Instruction: given a dialog context and related knowledge, you need to respond safely based on the knowledge."
        # Leave the knowldge empty
        query = input("You: ")
        dialog.append(query)

        knowledge = ""
        if args.wikidata:
            for word in get_proper_nouns(query):
                summaries = ""
                for result in search(word)[:1]:
                    summaries += get_summary(result["id"])

                matches = get_topn_similar(query, list(sentencer(summaries).sents), 10)
                for sentence in matches:
                    knowledge += f"{sentence} "

        if args.verbose:
            print(f"Knowledge: {knowledge}")

        response = generate(model, tokenizer, instruction, knowledge, dialog[:500])
        print(f"Computer: {response}")
