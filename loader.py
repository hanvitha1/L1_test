import os
import json
import random


def load_question_bank(base_path):
    """Loads questions from all JSON files in the specified directory."""
    question_bank = {}
    if not os.path.exists ( base_path ):
        print ( f"Error: Question bank directory '{base_path}' not found." )
        return question_bank

    for filename in os.listdir ( base_path ):
        if filename.endswith ( '.json' ):
            category_key = os.path.splitext ( filename )[0]  # e.g., "A_B", "C_D"
            filepath = os.path.join ( base_path, filename )
            try:
                with open ( filepath, 'r', encoding='utf-8' ) as f:
                    questions = json.load ( f )
                    question_bank[category_key] = questions
            except json.JSONDecodeError as e:
                print ( f"Error: Could not parse JSON from {filepath}: {e}" )
            except Exception as e:
                print ( f"Error loading questions from {filepath}: {e}" )

    print ( f"Loaded question bank categories: {list ( question_bank.keys () )}" )
    return question_bank


def get_random_questions(question_bank, category_key, num_questions):
    """Selects a random set of questions from a specific category."""
    questions = question_bank.get ( category_key, [] )
    if not questions:
        print ( f"Warning: No questions found for category '{category_key}'." )
        return []

    if len ( questions ) < num_questions:
        print (
            f"Warning: Not enough questions in category '{category_key}'. Requested {num_questions}, got {len ( questions )}." )
        return list ( questions )

    return random.sample ( questions, num_questions )