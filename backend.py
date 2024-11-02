from pydantic import BaseModel
from langchain.prompts import PromptTemplate
import pymongo
import traceback
import os, sys
from fastapi import FastAPI, UploadFile, status, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader
from langchain_community.callbacks.manager import get_openai_callback
from langchain.chains import ConversationalRetrievalChain
import gc
import awswrangler as wr
import boto3
import uuid
from typing import List
from dotenv import load_dotenv

if "OPENAI_API_BASE" in os.environ:
    del os.environ["OPENAI_API_BASE"]
load_dotenv()
# Environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
S3_KEY = os.getenv("S3_KEY")
S3_SECRET = os.getenv("S3_SECRET")
S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION")
S3_PATH = os.getenv("S3_PATH")
MONGO_URL = os.getenv("MONGO_URL")

try:
    client = pymongo.MongoClient(MONGO_URL, uuidRepresentation="standard")
    db = client["chat_with_doc"]
    conversationcol = db["chat-history"]
    conversationcol.create_index([("session_id")], unique=True)
except:
    print(traceback.format_exc())
    exc_type, exc_obj, exc_tb = sys.exc_info()
    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
    print(exc_type, fname, exc_tb.tb_lineno)


class ChatMessageSent(BaseModel):
    session_id: str = None
    user_input: str
    data_source: str


def get_response(
    file_name: str,
    session_id: str,
    query: str,
    model: str = "text-embedding-ada-002",
    temperature: float = 0,
):
    file_name = file_name.split("/")[-1]

    # Initialize Azure embeddings
    embeddings = AzureOpenAIEmbeddings(
        model="text-embedding-ada-002",
        azure_endpoint=AZURE_ENDPOINT,
        api_key=OPENAI_API_KEY,
        openai_api_version="2023-07-01-preview",
    )

    wr.s3.download(
        path=f"s3://openaitestawsbucket/coverletter/{file_name}",
        local_file=file_name,
        boto3_session=aws_s3,
    )
    print("file name is ", file_name)

    # Load document based on file type
    if file_name.endswith(".pdf"):
        loader = PyPDFLoader(file_name)
    elif file_name.endswith(".docx"):
        loader = Docx2txtLoader(file_name)
    else:
        raise ValueError("Unsupported file format. Please use PDF or DOCX files.")

    data = loader.load()
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=100, separators=["\n", " ", ""]
    )
    all_splits = text_splitter.split_documents(data)
    vectorstore = FAISS.from_documents(all_splits, embeddings)

    llm = AzureChatOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        openai_api_version="2023-07-01-preview",
        deployment_name="GPT4",
        openai_api_key=OPENAI_API_KEY,
        openai_api_type="azure",
        model_name=model,
        temperature=temperature,
    )

    qa_chain = ConversationalRetrievalChain.from_llm(
        llm, retriever=vectorstore.as_retriever(), condense_question_prompt=PromptTemplate.from_template(
        "You are a professional document analyzer. Please answer the following question based on the document content. Be direct and precise. If the information is not in the document, clearly state that. Remember, you are Bob, the analyzer, not the user asking the question.\n\nQuestion: {question}"

    ))

    # Get response with token tracking
    with get_openai_callback() as cb:
        answer = qa_chain.invoke(
            {
                "question": query,
                "chat_history": load_memory_to_pass(session_id=session_id),
            }
        )
        print(f"Total Tokens: {cb.total_tokens}")
        print(f"Prompt Tokens: {cb.prompt_tokens}")
        print(f"Completion Tokens: {cb.completion_tokens}")
        print(f"Total Cost (USD): ${cb.total_cost}")
        answer["total_tokens_used"] = cb.total_tokens

    gc.collect()
    return answer


def load_memory_to_pass(session_id: str):
    data = conversationcol.find_one({"session_id": session_id})
    history = []
    if data:
        data = data["conversation"]
        for x in range(0, len(data), 2):
            history.extend([(data[x], data[x + 1])])
    print("History: ", history)
    return history


def get_session() -> str:
    return str(uuid.uuid4())


def add_session_history(session_id: str, new_values: List):
    document = conversationcol.find_one({"session_id": session_id})
    if document:
        conversation = document["conversation"]
        conversation.extend(new_values)
        conversationcol.update_one(
            {"session_id": session_id}, {"$set": {"conversation": conversation}}
        )
    else:
        conversationcol.insert_one(
            {"session_id": session_id, "conversation": new_values}
        )


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

aws_s3 = boto3.Session(
    aws_access_key_id=S3_KEY,
    aws_secret_access_key=S3_SECRET,
    region_name=S3_REGION,
)

@app.post("/chat")
async def create_chat_message(chats: ChatMessageSent):
    try:
        if chats.session_id is None:
            session_id = get_session()
            payload = ChatMessageSent(
                session_id=session_id,
                user_input=chats.user_input,
                data_source=chats.data_source,
            )
            payload = payload.model_dump()

            response = get_response(
                file_name=payload.get("data_source"),
                session_id=payload.get("session_id"),
                query=payload.get("user_input"),
            )

            add_session_history(
                session_id=session_id,
                new_values=[payload.get("user_input"), response["answer"]],
            )

            return JSONResponse(
                content={
                    "response": response,
                    "session_id": str(session_id),
                }
            )

        else:
            payload = ChatMessageSent(
                session_id=str(chats.session_id),
                user_input=chats.user_input,
                data_source=chats.data_source,
            )
            payload = payload.dict()

            response = get_response(
                file_name=payload.get("data_source"),
                session_id=payload.get("session_id"),
                query=payload.get("user_input"),
            )

            add_session_history(
                session_id=str(chats.session_id),
                new_values=[payload.get("user_input"), response["answer"]],
            )

            return JSONResponse(
                content={
                    "response": response,
                    "session_id": str(chats.session_id),
                }
            )
    except Exception:
        print(traceback.format_exc())
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        print(exc_type, fname, exc_tb.tb_lineno)
        raise HTTPException(status_code=status.HTTP_204_NO_CONTENT, detail="error")


@app.post("/uploadFile")
async def upload_to_s3(data_file: UploadFile):
    # Extract the filename
    filename = data_file.filename.split("/")[-1]
    
    try:
        # Read the content of the uploaded file
        file_content = await data_file.read()

        # Upload the file directly to S3
        aws_s3.client('s3').put_object(
            Bucket=S3_BUCKET,
            Key=f"{S3_PATH}{filename}",
            Body=file_content,
            ContentType=data_file.content_type,  # Set the content type if needed
        )
        
        # Construct response
        s3_file_path = f"s3://{S3_BUCKET}/{S3_PATH}{filename}"
        return JSONResponse(content={"filename": filename, "file_path": s3_file_path})

    except Exception as e:
        # Capture any exceptions and return an error response
        print(f"Error uploading file: {str(e)}")
        raise HTTPException(status_code=500, detail="An error occurred while uploading the file.")


import uvicorn

if __name__ == "__main__":
    uvicorn.run("backend:app", reload=True)
