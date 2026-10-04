"""BTC 价格预测记录（`user_btc_predictions`，以 user_id 为主键：每人至多一条）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from core import sql


@dataclass(frozen=True, slots=True)
class Prediction:
    predict_type: str
    amount: int
    start_price: float
    start_time: datetime
    end_time: datetime


_COLUMNS = "predict_type, amount, start_price, start_time, end_time"


def _prediction_from_row(row: Row[Any]) -> Prediction:
    return Prediction(
        predict_type=str(row[0]),
        amount=int(row[1]),
        start_price=float(row[2]),
        start_time=row[3],
        end_time=row[4],
    )


async def get_running(
    user_id: int, now: datetime, *, connection: AsyncConnection | None = None
) -> Prediction | None:
    """还没结算、也没到结束时间的预测。"""
    row = await sql.fetch_one(
        f"SELECT {_COLUMNS} FROM user_btc_predictions "
        "WHERE user_id = %s AND is_completed = FALSE AND end_time > %s",
        (user_id, now),
        connection=connection,
    )
    return None if row is None else _prediction_from_row(row)


async def get_unsettled(
    user_id: int, *, connection: AsyncConnection | None = None
) -> Prediction | None:
    """还没结算的预测，不论是否已经到了结束时间。"""
    row = await sql.fetch_one(
        f"SELECT {_COLUMNS} FROM user_btc_predictions "
        "WHERE user_id = %s AND is_completed = FALSE",
        (user_id,),
        connection=connection,
    )
    return None if row is None else _prediction_from_row(row)


async def is_unsettled(connection: AsyncConnection, user_id: int, start_time: datetime) -> bool:
    """这条（以开始时间标识的）预测是否仍未结算。"""
    row = await sql.fetch_one(
        "SELECT 1 FROM user_btc_predictions "
        "WHERE user_id = %s AND is_completed = FALSE AND start_time = %s",
        (user_id, start_time),
        connection=connection,
    )
    return row is not None


async def mark_completed(connection: AsyncConnection, user_id: int) -> None:
    await connection.exec_driver_sql(
        "UPDATE user_btc_predictions SET is_completed = TRUE WHERE user_id = %s",
        (user_id,),
    )


async def complete_unsettled(connection: AsyncConnection, user_id: int) -> None:
    """只把还没结算的预测标记为完成。"""
    await connection.exec_driver_sql(
        "UPDATE user_btc_predictions SET is_completed = TRUE WHERE user_id = %s AND is_completed = FALSE",
        (user_id,),
    )


async def replace_prediction(
    connection: AsyncConnection,
    user_id: int,
    *,
    predict_type: str,
    amount: int,
    start_price: float,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """用新的预测取代用户已结算的旧记录（每人只保留一条）。"""
    await connection.exec_driver_sql(
        "DELETE FROM user_btc_predictions WHERE user_id = %s",
        (user_id,),
    )
    await connection.exec_driver_sql(
        "INSERT INTO user_btc_predictions (user_id, predict_type, amount, start_price, start_time, end_time) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, predict_type, amount, start_price, start_time, end_time),
    )
