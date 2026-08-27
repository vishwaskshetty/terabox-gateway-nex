import os
import sys

# Ensure 'src' is on sys.path so 'terabox_gateway' is importable when running from root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

# Directly import the Flask application from `terabox_gateway.api`
from terabox_gateway.api import app  # type: ignore

__all__ = ["app", "main"]


def main() -> None:
    """Run the Flask development server for local testing or container execution."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()

