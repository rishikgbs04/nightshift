"""Deterministic keyword extraction and ATS-style coverage scoring.

Uses a curated taxonomy of technologies and technical areas instead of an LLM,
so the same job description always yields the same keywords, the guard can
rely on it, and it costs nothing to run over hundreds of postings.

Each canonical term maps to aliases. Aliases are case-insensitive unless they
start with "=", which means "match case exactly" (for short, ambiguous words).
"""

from __future__ import annotations

import re
from functools import lru_cache

from .models import Coverage, Profile

TAXONOMY: dict[str, list[str]] = {
    # languages
    "Python": ["python"],
    "Java": ["java"],
    "C++": ["c++", "cpp"],
    "C#": ["c#", "csharp"],
    "JavaScript": ["javascript", "=JS", "ecmascript"],
    "TypeScript": ["typescript", "=TS"],
    "Go": ["golang", "=Go"],
    "Rust": ["=Rust"],
    "Kotlin": ["kotlin"],
    "Swift": ["=Swift"],
    "Scala": ["scala"],
    "Ruby": ["=Ruby"],
    "PHP": ["php"],
    "SQL": ["sql"],
    "Bash": ["bash", "shell scripting"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3"],
    "MATLAB": ["matlab"],
    # web / frameworks
    "React": ["react", "react.js", "reactjs"],
    "React Native": ["react native"],
    "Next.js": ["next.js", "nextjs"],
    "Vue": ["vue", "vue.js"],
    "Angular": ["angular"],
    "Node.js": ["node.js", "nodejs", "=Node"],
    "Express": ["express.js", "expressjs"],
    "Redux": ["redux"],
    "GraphQL": ["graphql"],
    "REST APIs": ["rest api", "rest apis", "restful", "=REST"],
    "gRPC": ["grpc"],
    "FastAPI": ["fastapi"],
    "Flask": ["flask"],
    "Django": ["django", "django rest framework", "drf"],
    "Spring": ["spring boot", "=Spring"],
    ".NET": [".net", "asp.net"],
    "Pydantic": ["pydantic"],
    "Jest": ["jest"],
    "pytest": ["pytest"],
    "JUnit": ["junit"],
    "Selenium": ["selenium"],
    "Playwright": ["playwright"],
    # data / ml
    "Machine Learning": ["machine learning", "=ML"],
    "Deep Learning": ["deep learning"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "image recognition"],
    "LLMs": ["llm", "llms", "large language model", "large language models"],
    "RAG": ["=RAG", "retrieval-augmented generation", "retrieval augmented generation"],
    "Embeddings": ["embedding", "embeddings", "vector search", "semantic search"],
    "Prompt Engineering": ["prompt engineering"],
    "AI Agents": ["ai agent", "ai agents", "agentic"],
    "PyTorch": ["pytorch"],
    "TensorFlow": ["tensorflow"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "NumPy": ["numpy"],
    "Pandas": ["pandas"],
    "Spark": ["spark", "pyspark", "apache spark"],
    "Hadoop": ["hadoop"],
    "Airflow": ["airflow", "apache airflow"],
    "Kafka": ["kafka"],
    "ETL": ["etl", "elt", "data pipeline", "data pipelines"],
    "Data Visualization": ["data visualization", "info visualization", "information visualization", "d3.js", "=D3"],
    "Statistics": ["statistics", "statistical"],
    "A/B Testing": ["a/b testing", "a/b tests", "experimentation"],
    # databases
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "SQLite": ["sqlite"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "DynamoDB": ["dynamodb"],
    "Cassandra": ["cassandra"],
    "Elasticsearch": ["elasticsearch", "opensearch"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery"],
    "Firebase": ["firebase", "firestore"],
    "Vector Databases": ["vector database", "vector databases", "pinecone", "pgvector", "faiss"],
    # cloud / infra
    "AWS": ["aws", "amazon web services", "=EC2", "=S3", "lambda functions"],
    "GCP": ["gcp", "google cloud", "dataproc"],
    "Azure": ["azure"],
    "Docker": ["docker", "containerization", "containers"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform", "infrastructure as code"],
    "CI/CD": ["ci/cd", "continuous integration", "continuous deployment", "github actions", "jenkins", "gitlab ci"],
    "GitHub Actions": ["github actions"],
    "Linux": ["linux", "unix"],
    "Git": ["=git", "=Git", "github", "version control"],
    "Prometheus": ["prometheus"],
    "Grafana": ["grafana"],
    "Observability": ["observability", "monitoring", "metrics", "structured logging", "tracing"],
    "Serverless": ["serverless", "cloud functions", "aws lambda"],
    # concepts
    "Distributed Systems": ["distributed systems", "distributed system"],
    "Microservices": ["microservice", "microservices"],
    "System Design": ["system design", "systems design"],
    "Concurrency": ["concurrency", "multi-threaded", "multithreaded", "multithreading", "parallelism"],
    "Caching": ["caching", "cache", "semantic cache"],
    "Rate Limiting": ["rate limit", "rate limits", "rate limiting", "token bucket", "token-bucket"],
    "Load Balancing": ["load balancing", "load balancer"],
    "Fault Tolerance": ["failover", "fault tolerance", "fault-tolerant", "circuit breaker", "circuit-breaker", "high availability"],
    "Performance Optimization": ["performance optimization", "latency", "load speed", "throughput"],
    "Authentication": ["authentication", "oauth", "sso", "auth"],
    "Security": ["security", "cryptography", "post-quantum"],
    "Unit Testing": ["unit test", "unit tests", "unit testing", "test coverage", "automated testing", "software testing"],
    "Test Automation": ["test automation", "automation testing", "automated testing"],
    "Agile": ["agile", "scrum", "sprint", "sprints"],
    "UML": ["uml"],
    "Object-Oriented Design": ["object-oriented", "oop", "object oriented"],
    "Data Structures & Algorithms": ["data structures", "algorithms"],
    "APIs": ["=API", "=APIs", "api design"],
    "Frontend": ["front-end", "frontend", "front end"],
    "Backend": ["back-end", "backend", "back end"],
    "Full-Stack": ["full-stack", "full stack", "fullstack"],
    "Mobile": ["mobile app", "ios", "android"],
    "Geospatial": ["geospatial", "google maps", "gis"],
    "Big Data": ["big data"],
}

_BOUNDARY_L = r"(?<![A-Za-z0-9+#])"
_BOUNDARY_R = r"(?![A-Za-z0-9+#])"


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for canon, aliases in TAXONOMY.items():
        ci, cs = [], []
        for a in aliases:
            (cs if a.startswith("=") else ci).append(re.escape(a.lstrip("=")))
        if ci:
            out.append((canon, re.compile(_BOUNDARY_L + "(?:" + "|".join(ci) + ")" + _BOUNDARY_R, re.I)))
        if cs:
            out.append((canon, re.compile(_BOUNDARY_L + "(?:" + "|".join(cs) + ")" + _BOUNDARY_R)))
    return out


_COMMON_NOUNS = {
    "Machine Learning", "Deep Learning", "Embeddings", "AI Agents", "Prompt Engineering", "Statistics",
    "Data Visualization", "Vector Databases", "Observability", "Serverless", "Distributed Systems",
    "Microservices", "System Design", "Concurrency", "Caching", "Rate Limiting", "Load Balancing",
    "Fault Tolerance", "Performance Optimization", "Authentication", "Security", "Unit Testing",
    "Test Automation", "Object-Oriented Design", "Data Structures & Algorithms", "Frontend", "Backend",
    "Full-Stack", "Mobile", "Geospatial", "Big Data", "Computer Vision",
}


def display(term: str) -> str:
    """How to write a term mid-sentence ("caching", but "PostgreSQL")."""
    return term.lower() if term in _COMMON_NOUNS else term


def find_terms(text: str) -> set[str]:
    """Canonical taxonomy terms mentioned in text."""
    return {canon for canon, pat in _compiled() if pat.search(text)}


def term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for canon, pat in _compiled():
        n = len(pat.findall(text))
        if n:
            counts[canon] = counts.get(canon, 0) + n
    return counts


def jd_keywords(jd: str) -> list[str]:
    """JD keywords, most-mentioned first."""
    counts = term_counts(jd)
    return sorted(counts, key=lambda k: (-counts[k], k))


def coverage(jd: str, resume_text: str, profile: Profile) -> Coverage:
    kws = jd_keywords(jd)
    in_resume = find_terms(resume_text)
    in_profile = find_terms(profile.full_text())
    matched = [k for k in kws if k in in_resume]
    missing_in_profile = [k for k in kws if k not in in_resume and k in in_profile]
    gaps = [k for k in kws if k not in in_profile]
    coverable = [k for k in kws if k in in_profile]
    score = (len([k for k in coverable if k in in_resume]) / len(coverable)) if coverable else 0.0
    # Recruiters skim the top of the page: how many coverable keywords show up
    # in the first 40% of the experience/projects text?
    top = find_terms(resume_text[: int(len(resume_text) * 0.4)])
    prominence = (len([k for k in coverable if k in top]) / len(coverable)) if coverable else 0.0
    return Coverage(
        jd_keywords=kws,
        matched=matched,
        missing_but_in_profile=missing_in_profile,
        gaps=gaps,
        score=round(score, 3),
        prominence=round(prominence, 3),
    )


def profile_fit(jd: str, profile: Profile) -> float:
    """Cheap 0..1 pre-score: share of the JD's keywords your profile has at all."""
    kws = jd_keywords(jd)
    if not kws:
        return 0.0
    have = find_terms(profile.full_text())
    return round(sum(1 for k in kws if k in have) / len(kws), 3)
