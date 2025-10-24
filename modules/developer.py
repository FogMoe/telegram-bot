import logging
import os
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
import mysql.connector
import mysql_connection
import tempfile
from command_cooldown import cooldown # 导入冷却装饰器
import config

# 定义开发者命令处理函数

@cooldown # 添加冷却装饰器
async def get_bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示机器人当前服务的部分统计信息和群组ID列表"""
    
    # 检查使用者是否为管理员
    if update.effective_user.id != config.ADMIN_USER_ID: # ADMIN_USER_ID
        await update.message.reply_text("您没有权限执行此操作")
        return
    
    try:
        # 连接数据库
        connection = mysql_connection.create_connection()
        cursor = connection.cursor(dictionary=True) # 使用字典游标方便获取列名
        
        # --- 获取统计计数 ---
        cursor.execute("SELECT COUNT(*) as count FROM user")
        user_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(DISTINCT group_id) as count FROM group_keywords")
        keyword_group_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM group_verification")
        verify_group_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM group_spam_control WHERE enabled = TRUE")
        spam_group_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(DISTINCT group_id) as count FROM group_chart_tokens")
        chart_group_count = cursor.fetchone()['count']
        
        # --- 获取群组 ID 列表 (限制最多20个) ---
        limit = 20
        cursor.execute(f"SELECT DISTINCT group_id FROM group_keywords LIMIT {limit}")
        keyword_group_ids = [str(row['group_id']) for row in cursor.fetchall()]
        
        cursor.execute(f"SELECT group_id FROM group_verification LIMIT {limit}") # verification表主键是group_id, 无需distinct
        verify_group_ids = [str(row['group_id']) for row in cursor.fetchall()]
        
        cursor.execute(f"SELECT group_id FROM group_spam_control WHERE enabled = TRUE LIMIT {limit}") # spam表主键是group_id, 无需distinct
        spam_group_ids = [str(row['group_id']) for row in cursor.fetchall()]
        
        cursor.execute(f"SELECT DISTINCT group_id FROM group_chart_tokens LIMIT {limit}")
        chart_group_ids = [str(row['group_id']) for row in cursor.fetchall()]

        # --- 获取用户列表 (按ID降序，作为最近用户) ---
        cursor.execute("""
            SELECT id, name
            FROM user 
            ORDER BY id DESC 
            LIMIT 10
        """)
        recent_users = cursor.fetchall()
        
        # --- 构建统计信息消息 ---
        stats_message = f"🤖 *机器人统计信息*\n\n"
        stats_message += f"👤 总用户数: {user_count}\n"
        stats_message += f"💬 配置关键词群组: {keyword_group_count}\n"
        stats_message += f"✅ 启用验证群组: {verify_group_count}\n"
        stats_message += f"🛡️ 启用垃圾控制群组: {spam_group_count}\n"
        stats_message += f"📈 配置图表群组: {chart_group_count}\n\n"
        
        # 添加最近用户信息
        stats_message += "*最近的用户 (按ID排序，最多10个):*\n"
        if recent_users:
            for user in recent_users:
                # 使用数据库中的 'name' 字段
                user_info = f"ID: {user['id']}, Name: {user['name']}"
                stats_message += f"- {user_info}\n"
        else:
            stats_message += "无\n"

        # 添加群组 ID 列表
        stats_message += f"\n*使用各项功能的群组 ID (最多{limit}个):*\n"
        stats_message += f"💬 关键词: `{', '.join(keyword_group_ids) if keyword_group_ids else '无'}`\n"
        stats_message += f"✅ 验证: `{', '.join(verify_group_ids) if verify_group_ids else '无'}`\n"
        stats_message += f"🛡️ 垃圾控制: `{', '.join(spam_group_ids) if spam_group_ids else '无'}`\n"
        stats_message += f"📈 图表: `{', '.join(chart_group_ids) if chart_group_ids else '无'}`\n"

        # 关闭数据库连接
        cursor.close()
        connection.close()
        
        # 发送消息 (如果太长可能需要分段或发文件)
        if len(stats_message) > 4000:
            await update.message.reply_text("统计信息过长，将以文件形式发送。")
            # 可以考虑将 stats_message 写入临时文件发送
            try:
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.md', delete=False, encoding='utf-8') as temp_file:
                    temp_file.write(stats_message)
                    temp_file_path = temp_file.name
                with open(temp_file_path, 'rb') as f:
                    await update.message.reply_document(document=f, filename="bot_stats.md")
                os.remove(temp_file_path)
            except Exception as file_e:
                logging.error(f"发送统计文件出错: {file_e}")
                await update.message.reply_text("发送统计文件时出错。")
        else:
           await update.message.reply_text(stats_message, parse_mode='Markdown')
        
    except mysql.connector.Error as db_err:
        logging.error(f"数据库查询出错: {str(db_err)}")
        await update.message.reply_text(f"数据库查询出错: {str(db_err)}")
    except Exception as e:
        logging.error(f"获取统计信息出错: {str(e)}")
        await update.message.reply_text(f"获取统计信息出错: {str(e)}")

@cooldown # 添加冷却装饰器
async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示机器人最近的日志"""
    
    # 检查使用者是否为管理员
    if update.effective_user.id != config.ADMIN_USER_ID:  # ADMIN_USER_ID
        await update.message.reply_text("您没有权限执行此操作")
        return
    
    try:
        # 获取日志行数参数，默认为50行
        lines = 50
        if context.args and context.args[0].isdigit():
            lines = min(int(context.args[0]), 200)  # 限制最多显示200行
        
        # 读取日志文件的最后N行
        log_path = 'tgbot.log'
        if not os.path.exists(log_path):
            await update.message.reply_text("日志文件不存在")
            return
        
        # 读取最后N行日志
        with open(log_path, 'r', encoding='utf-8') as f:
            log_lines = f.readlines()
            last_logs = log_lines[-lines:] if len(log_lines) > lines else log_lines
        
        # 构建日志消息
        logs_message = f"📋 *最近{len(last_logs)}行日志*\n\n```\n"
        logs_message += ''.join(last_logs)
        logs_message += "\n```"
        
        # 如果日志太长，分段发送或发文件
        if len(logs_message) > 4000:
            await update.message.reply_text("日志内容过长，将以文件形式发送")
            try:
                with tempfile.NamedTemporaryFile(mode='w+', suffix='.log', delete=False, encoding='utf-8') as temp_file:
                    temp_file.write("".join(last_logs))
                    temp_file_path = temp_file.name
                with open(temp_file_path, 'rb') as f:
                    await update.message.reply_document(document=f, filename="bot_logs.log")
                os.remove(temp_file_path)
            except Exception as file_e:
                logging.error(f"发送日志文件出错: {file_e}")
                await update.message.reply_text("发送日志文件时出错。")
        else:
            await update.message.reply_text(logs_message, parse_mode='Markdown')
            
    except Exception as e:
        logging.error(f"获取日志出错: {str(e)}")
        await update.message.reply_text(f"获取日志出错: {str(e)}")

# 设置开发者命令处理器
def setup_developer_handlers(application):
    """设置开发者命令处理器"""
    application.add_handler(CommandHandler("stats", get_bot_stats))
    application.add_handler(CommandHandler("logs", view_logs))
    logging.info("开发者命令模块已加载")
