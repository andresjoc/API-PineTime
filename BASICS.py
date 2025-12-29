from enum import IntEnum
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

api = FastAPI()

class Priority(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

class TodoBase(BaseModel): #To define squema
    todo_name: str = Field(..., min_length=3, max_length=512, description="Name of the todo item")
    todo_description: str = Field(..., description="Description of the todo item")
    priority: Priority = Field(default=Priority.LOW, description="Priority level of the todo item")

class TodoCreate(TodoBase):
    pass

class Todo(TodoBase): # As a response model
    todo_id: int = Field(..., description="Unique identifier for the todo item")
    pass

class TodoUpdate(BaseModel):
    todo_name: Optional[str] = Field(None, min_length=3, max_length=512, description="Name of the todo item")
    todo_description: Optional[str] = Field(None, description="Description of the todo item")
    priority: Optional[Priority] = Field(None, description="Priority level of the todo item")






#Not an actual DB just dictionary to simulate data storage

all_todos = [
    Todo(todo_id=1, todo_name="Buy groceries", todo_description="Milk, Bread, Eggs", priority=Priority.MEDIUM),
    Todo(todo_id=2, todo_name="Read a book", todo_description="Finish reading '1984' by George Orwell", priority=Priority.LOW),
    Todo(todo_id=3, todo_name="Workout", todo_description="Go for a 30-minute run", priority=Priority.HIGH),
    Todo(todo_id=4, todo_name="Call Mom", todo_description="Check in with Mom and see how she's doing", priority=Priority.MEDIUM),
    Todo(todo_id=5, todo_name="Clean House", todo_description="Vacuum and dust all rooms", priority=Priority.LOW)
]




# GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD

@api.get("/")
def index():
    return {"message": "Hello, World!"}







@api.get("/todos/{todo_id}", response_model=Todo) # Path Parameter
def get_todo(todo_id: int):
    for todo in all_todos:
        if todo.todo_id == todo_id:
            return todo
    raise HTTPException(status_code=404, detail="Todo not found")
        
@api.get('/todos', response_model=List[Todo]) # Query Parameter /todos?first_n=2
def get_todos(first_n: int = None): # It's important to specify types
    if first_n:
        return all_todos[:first_n]
    else:
        return all_todos


@api.post('/todos', response_model=Todo)
def create_todo(todo: TodoCreate): # TodoCreate does not have todo_id
    new_todo_id = max(todo.todo_id for todo in all_todos) + 1

    new_todo = Todo(
        todo_id=new_todo_id,
        todo_name=todo.todo_name,
        todo_description=todo.todo_description,
        priority=todo.priority)


    all_todos.append(new_todo)

    return new_todo

@api.put('/todos/{todo_id}', response_model=Todo)
def update_todo(todo_id: int, updated_todo: TodoUpdate):
    for todo in all_todos:
        if todo.todo_id == todo_id:
            if updated_todo.todo_name is not None:
                todo.todo_name = updated_todo.todo_name
            if updated_todo.todo_description is not None:
                todo.todo_description = updated_todo.todo_description
            if updated_todo.priority is not None:
                todo.priority = updated_todo.priority
            return todo
    raise HTTPException(status_code=404, detail="Todo not found")

@api.delete('/todos/{todo_id}', response_model=Todo)
def delete_todo(todo_id: int):
    for index, todo in enumerate(all_todos):
        if todo.todo_id == todo_id:
            deleted_todo = all_todos.pop(index)
            return deleted_todo
    raise HTTPException(status_code=404, detail="Todo not found")

