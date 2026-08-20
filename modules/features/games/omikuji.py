import random
import hashlib
import time
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import telegram
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from core import mysql_connection, process_user
from core.command_cooldown import cooldown

# 防止用户快速多次点击的锁
# 格式: {user_id: lock_until_timestamp}
omikuji_locks = {}

# 添加日志记录
logger = logging.getLogger(__name__)

# 运势级别及其对应解释（从最好到最差）
OMIKUJI_FORTUNES = {
    "大吉": {
        "description": [
            "这是最高等级的好运，今天的一切都会顺利进行。",
            "福气满满，万事如意，今天将是你的幸运日。",
            "吉星高照，前途光明，今日诸事皆宜。",
            "天降祥瑞，百事亨通，天佑之人莫过于你。"
        ],
        "health": [
            "身体健康充满活力，远离疾病。",
            "气血充盈，精力十足，是强健体魄的好时机。",
            "身轻如燕，神清气爽，百病不侵。",
            "健康状态极佳，宜适当运动增强体质。"
        ],
        "love": [
            "爱情方面可能会有意外的惊喜，单身者可能遇到心仪的对象。",
            "桃花运旺盛，感情融洽，情侣间增进感情的好时机。",
            "缘分天定，有望遇见对的人，或与伴侣关系更进一步。",
            "柔情蜜意，心有灵犀，感情生活甜蜜幸福。"
        ],
        "career": [
            "事业上会有重大突破，努力将得到回报。",
            "贵人相助，事业腾飞，有望收获意外之喜。",
            "独具慧眼，思维敏捷，工作中的表现将得到赞赏。",
            "机遇降临，能力获得认可，是升职加薪的好兆头。"
        ],
        "study": [
            "学习效率极高，记忆力增强，是考试和深入学习的好时机。",
            "思路清晰，理解力强，学习新知识事半功倍。",
            "专注力提升，善于举一反三，能够融会贯通。",
            "学习热情高涨，善于思考，知识吸收效率大增。"
        ],
        "advice": [
            "充分利用今天的好运，大胆追求自己的目标。",
            "锐意进取，抓住机会，勇敢向前，必有收获。",
            "相信自己，勇往直前，吉星相助，无往不利。",
            "积极行动，把握当下，好运当头，事事顺心。"
        ]
    },
    "中吉": {
        "description": [
            "运势很好，虽然不是最顶级，但也足够让你度过美好的一天。",
            "福运亨通，诸事顺遂，今天将会充满惊喜。",
            "吉祥如意，心想事成，令人喜悦的一天。",
            "好运连连，顺风顺水，是适合行动的时机。"
        ],
        "health": [
            "身体状况良好，保持适当运动可以更加健康。",
            "精神饱满，活力四射，宜适量锻炼身体。",
            "身体机能运转良好，注意作息规律更佳。",
            "体力充沛，抵抗力强，保持规律生活习惯。"
        ],
        "love": [
            "感情稳定发展，与伴侣沟通顺畅。",
            "情感和谐，互相尊重，是加深感情的好时机。",
            "心意相通，情投意合，恋情稳步向前发展。",
            "缘分际会，真心相待，感情生活温馨甜蜜。"
        ],
        "career": [
            "工作顺利，可能会得到上司的赏识。",
            "事业有成，同事信任，工作中将取得不错成绩。",
            "能力得到发挥，工作效率高，有望获得肯定。",
            "职场人缘好，合作顺利，工作氛围和谐。"
        ],
        "study": [
            "学习有所进步，思路清晰。",
            "求知欲强，思维活跃，学习成效显著。",
            "专注用功，心无旁骛，知识积累稳步提升。",
            "悟性良好，善于吸收，学习过程顺畅有效。"
        ],
        "advice": [
            "保持积极心态，继续坚持当前的努力方向。",
            "稳步前进，脚踏实地，持之以恒方能成功。",
            "善用智慧，积极行动，必能获得满意成果。",
            "把握机会，用心经营，努力终将不负所望。"
        ]
    },
    "小吉": {
        "description": [
            "运势偏好，可能会有一些小小的好事发生。",
            "微微向好，虽无大喜，但有小幸运降临。",
            "平稳中带着些许好运，保持平常心即可。",
            "整体向上，虽不惊艳，但足以带来些许欢乐。"
        ],
        "health": [
            "身体无大碍，但需要注意休息。",
            "体质尚可，适当调养可增强免疫力。",
            "健康状况稳定，注意劳逸结合更佳。",
            "无明显不适，保持充足睡眠有益健康。"
        ],
        "love": [
            "感情生活平稳，需要更多关心对方。",
            "情感世界安稳，多一些体贴可增进感情。",
            "感情基础牢固，适当的浪漫可锦上添花。",
            "缘分随缘，顺其自然，真心相待终有回报。"
        ],
        "career": [
            "工作中可能有小成就，但也要防止骄傲。",
            "职场表现尚可，踏实做事必有回报。",
            "事业稳中有进，细心专注可避免小错。",
            "工作态度认真，得到同事认可，继续保持。"
        ],
        "study": [
            "学习有效率，但需要更加专注。",
            "求知态度端正，加强自律可提高效率。",
            "学习节奏平稳，保持恒心可见长期成效。",
            "知识吸收有序，制定计划有助于进步。"
        ],
        "advice": [
            "脚踏实地，不要急于求成。",
            "稳扎稳打，循序渐进，水滴石穿终有成。",
            "保持平常心，积累经验，厚积薄发方为上策。",
            "细心谨慎，勤勉不懈，日积月累必有所获。"
        ]
    },
    "末吉": {
        "description": [
            "运势一般，不好不坏，需要谨慎行事。",
            "平平淡淡，波澜不惊，平稳度过即为幸运。",
            "喜忧参半，起伏不定，凡事谨慎为上。",
            "不温不火，不咸不淡，中规中矩的一天。"
        ],
        "health": [
            "注意身体，避免过度疲劳。",
            "体质偏弱，宜静养调息，勿过度劳累。",
            "健康状况中等，注意饮食规律为宜。",
            "身体略感疲惫，适当休息可恢复活力。"
        ],
        "love": [
            "感情上可能有些小波折，需要耐心沟通。",
            "情感道路略有坎坷，包容理解是维系的关键。",
            "感情需要经营，真诚对待可化解误会。",
            "缘分考验，互相尊重，坦诚相待可度过难关。"
        ],
        "career": [
            "工作中会遇到一些挑战，保持冷静应对。",
            "职场小波折，临危不乱，沉着应对可转危为安。",
            "事业发展遇到瓶颈，调整思路寻找突破。",
            "工作压力增大，条理分明，稳步推进为佳。"
        ],
        "study": [
            "学习效果一般，需要调整方法提高效率。",
            "学习进度缓慢，重整思路，回归基础可取得进步。",
            "知识吸收不畅，适当放松，换个角度或许豁然开朗。",
            "注意力不集中，制定短期目标，循序渐进为宜。"
        ],
        "advice": [
            "凡事三思而后行，不要冲动决策。",
            "谨慎行事，量力而行，静待时机再出发。",
            "冷静思考，理性决策，稳妥处理更为可靠。",
            "保持耐心，韬光养晦，蓄势待发才能一鸣惊人。"
        ]
    },
    "凶": {
        "description": [
            "运势不佳，可能会遇到一些麻烦。",
            "诸事不顺，困难重重，需谨慎应对。",
            "挫折连连，障碍频现，保持冷静为上。",
            "逆境降临，暂时低迷，静待时机好转。"
        ],
        "health": [
            "身体可能感到不适，应该多注意休息。",
            "体质欠佳，易感疲倦，宜多休息少操劳。",
            "健康状况令人担忧，应加强保健避免恶化。",
            "容易感到不适，及时调整作息，预防胜于治疗。"
        ],
        "love": [
            "感情可能会有矛盾，需要多一些包容和理解。",
            "情感危机，误会加深，需要冷静处理避免冲突。",
            "感情进入低谷，保持距离冷静思考为宜。",
            "缘分考验，风雨同舟，真情才能经得起考验。"
        ],
        "career": [
            "工作中可能会遇到困难，需要谨慎处理。",
            "事业受阻，挑战重重，沉着应对方能转机。",
            "职场不顺，暗流涌动，低调行事以避锋芒。",
            "工作压力巨大，量力而行，避免勉强冒进。"
        ],
        "study": [
            "学习效果不理想，可能注意力不集中。",
            "学习遇到瓶颈，难以突破，应调整心态重新开始。",
            "知识吸收困难，效率低下，适当休息再战。",
            "思维混乱，难以专注，放慢节奏找回学习状态。"
        ],
        "advice": [
            "放松心态，遇事不要太过着急，等待好时机。",
            "暂避锋芒，韬光养晦，静待云开见月明。",
            "谨慎行事，减少冒险，守成为主以避损失。",
            "退一步海阔天空，忍一时风平浪静。"
        ]
    },
    "大凶": {
        "description": [
            "运势很差，可能会遇到较大的困难。",
            "厄运当头，诸事不顺，暂时难见转机。",
            "危机四伏，处处受阻，需谨慎应对。",
            "困难重重，挫折连连，保持镇定度过难关。"
        ],
        "health": [
            "身体可能会感到不适，应该注意休息并避免剧烈运动。",
            "体质虚弱，易生病痛，宜安心静养避免劳累。",
            "健康状况堪忧，建议及时调整生活方式。",
            "容易感到疲惫不堪，应减少活动，注重休息。"
        ],
        "love": [
            "感情可能会遇到严重的挫折，需要冷静思考。",
            "情感危机严重，争吵不断，建议暂时冷静思考。",
            "感情跌入低谷，误会加深，需要给彼此空间。",
            "缘分考验剧烈，唯有真心才能共渡难关。"
        ],
        "career": [
            "工作中可能会遇到重大障碍，需要寻求他人帮助。",
            "事业遭受重创，困难重重，需要沉着冷静面对。",
            "职场危机，阻力巨大，建议暂避锋芒。",
            "工作压力极大，暂时无法突破，适当放低期望。"
        ],
        "study": [
            "学习效果很差，可能很难集中注意力。",
            "学习困难重重，难以进步，需要彻底调整方法。",
            "知识理解障碍，效率极低，建议暂时休整。",
            "思维混乱不堪，无法专注，应当放下重担再出发。"
        ],
        "advice": [
            "今天应尽量避免重大决策，保持低调，等待运势好转。",
            "危机当前，静观其变，不妄动不强求。",
            "避开风头，减少风险，退守为上策。",
            "修身养性，积蓄能量，蛰伏以待时机成熟。"
        ]
    }
}

# 运势概率分布
FORTUNE_WEIGHTS = {
    "大吉": 10,   # 10%
    "中吉": 20,   # 20%
    "小吉": 30,   # 30%
    "末吉": 20,   # 20%
    "凶": 15,     # 15%
    "大凶": 5     # 5%
}


def get_daily_fortune(user_id: int) -> str:
    """
    基于用户ID和当前日期确定用户的每日运势
    """
    # 获取当前日期（年月日）
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 组合用户ID和日期作为随机种子
    seed = f"{user_id}_{today}"
    
    # 使用哈希函数生成一个确定性的数值
    hash_value = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    random.seed(hash_value)
    
    # 根据权重选择运势
    fortunes = list(FORTUNE_WEIGHTS.keys())
    weights = list(FORTUNE_WEIGHTS.values())
    
    # 选择运势
    fortune = random.choices(fortunes, weights=weights, k=1)[0]
    
    # 重置随机种子
    random.seed()
    
    return fortune


async def check_and_deduct_coins(user_id: int) -> bool:
    """检查用户是否有足够的金币并扣除"""
    try:
        async with mysql_connection.transaction() as connection:
            user = await mysql_connection.fetch_one(
                "SELECT id, coins, coins_paid FROM user WHERE id = %s",
                (user_id,),
                connection=connection,
            )

            if not user:
                logger.warning(f"用户 {user_id} 不存在")
                return False

            current_coins = (user[1] or 0) + (user[2] or 0)
            if current_coins < 1:
                logger.info(f"用户 {user_id} 金币不足，当前金币: {current_coins}")
                return False
            spent = await process_user.spend_user_coins(
                user_id,
                1,
                connection=connection,
            )
            if not spent:
                logger.info(f"用户 {user_id} 金币不足，当前金币: {current_coins}")
                return False
            logger.info(f"用户 {user_id} 扣除1金币成功，剩余金币: {current_coins - 1}")
            return True
    except Exception as e:
        logger.error(f"扣除金币时出错: {str(e)}")
        return False


async def get_user_daily_fortune(user_id: int):
    """
    从数据库获取用户当天的抽签记录
    如果存在记录，返回(True, fortune)
    如果不存在记录，返回(False, None)
    """
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        result = await mysql_connection.fetch_one(
            "SELECT fortune FROM user_omikuji WHERE user_id = %s AND fortune_date = %s",
            (user_id, today),
        )

        if result:
            logger.info(f"用户 {user_id} 今日已抽签，结果: {result[0]}")
            return True, result[0]
        logger.info(f"用户 {user_id} 今日尚未抽签")
        return False, None
    except Exception as e:
        logger.error(f"获取用户抽签记录时出错: {str(e)}")
        return False, None


async def save_user_fortune(user_id: int, fortune: str) -> bool:
    """保存用户的抽签记录到数据库"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        await mysql_connection.execute(
            "INSERT INTO user_omikuji (user_id, fortune_date, fortune) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE fortune = VALUES(fortune)",
            (user_id, today, fortune),
        )
        logger.info(f"用户 {user_id} 抽签结果 {fortune} 已保存")
        return True
    except Exception as e:
        logger.error(f"保存用户抽签记录时出错: {str(e)}")
        return False


async def check_user_registered(user_id: int) -> bool:
    """检查用户是否已注册"""
    try:
        result = await mysql_connection.fetch_one(
            "SELECT id FROM user WHERE id = %s",
            (user_id,),
        )
        is_registered = result is not None
        if not is_registered:
            logger.info(f"用户 {user_id} 未注册")
        return is_registered
    except Exception as e:
        logger.error(f"检查用户注册状态时出错: {str(e)}")
        return False


@cooldown
async def omikuji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /omikuji 命令
    """
    try:
        user_id = update.effective_user.id
        user_name = update.effective_user.username or update.effective_user.first_name
        
        logger.info(f"用户 {user_id} ({user_name}) 请求抽签")
        
        # 检查用户是否注册
        is_registered = await check_user_registered(user_id)
        
        if not is_registered:
            await update.message.reply_text(
                "您需要先注册个人信息才能使用御神签功能。\n"
                "请使用 /me 命令完成注册后再来抽签吧！\n\n"
                "You need to register first before drawing an omikuji.\n"
                "Please use the /me command to register and then try again!"
            )
            return
        
        # 检查是否存在锁定状态（防止快速多次点击）
        current_time = time.time()
        if user_id in omikuji_locks and omikuji_locks[user_id] > current_time:
            await update.message.reply_text(
                "请不要频繁抽签，神明需要休息...请稍等片刻再试。\n"
                "Please don't draw omikuji too frequently, the gods need rest... Try again in a moment."
            )
            return
        
        # 设置3秒锁定
        omikuji_locks[user_id] = current_time + 3
        
        # 检查用户今天是否已经抽过签
        has_drawn, existing_fortune = await get_user_daily_fortune(user_id)
        
        if has_drawn:
            # 用户今天已经抽过签，直接获取已有结果
            fortune = existing_fortune
            if fortune not in OMIKUJI_FORTUNES:
                logger.error(f"用户 {user_id} 的运势记录 {fortune} 无效")
                await update.message.reply_text(
                    "抱歉，您的运势记录出现错误。请联系管理员或明天再试。\n"
                    "Sorry, there was an error with your fortune record. Please contact admin or try again tomorrow."
                )
                return
                
            fortune_info = OMIKUJI_FORTUNES[fortune]
            
            # 准备消息内容 - 使用相同的随机数生成器以确保展示与第一次相同
            seed_value = int(hashlib.md5(f"{user_id}_{datetime.now().strftime('%Y-%m-%d')}".encode()).hexdigest(), 16)
            random_gen = random.Random(seed_value)
            
            # 修改消息格式，避免特殊字符问题
            message = (
                f"🔮 {user_name}的今日运势 🔮\n\n"
                f"结果: {fortune}\n\n"
                f"{random_gen.choice(fortune_info['description'])}\n\n"
                f"健康: {random_gen.choice(fortune_info['health'])}\n"
                f"爱情: {random_gen.choice(fortune_info['love'])}\n"
                f"事业/学业: {random_gen.choice(fortune_info['career'])}\n\n"
                f"建议: {random_gen.choice(fortune_info['advice'])}\n\n"
                f"您今天已经抽过御神签了。每人每天只能抽取一次，明天再来吧！\n"
                f"You have already drawn an omikuji today. One draw per person per day, come back tomorrow!"
            )
            
            # 尝试使用Markdown，如果失败则回退到纯文本
            try:
                await update.message.reply_text(
                    message,
                    parse_mode="MARKDOWN"
                )
            except telegram.error.BadRequest as e:
                logger.warning(f"Markdown格式发送失败，切换到纯文本: {e}")
                await update.message.reply_text(message)
            return
        
        # 检查并扣除金币
        coins_deducted = await check_and_deduct_coins(user_id)
        
        if not coins_deducted:
            await update.message.reply_text(
                "您没有足够的金币进行祈愿抽签。每次抽签需要1枚金币作为供奉。\n"
                "试试使用 /lottery 命令获取免费金币吧！\n\n"
                "You don't have enough coins to draw an omikuji. Each draw requires 1 coin as an offering.\n"
                "Try using /lottery command to get free coins!"
            )
            return
        
        # 获取用户的每日运势
        fortune = get_daily_fortune(user_id)
        fortune_info = OMIKUJI_FORTUNES[fortune]
        
        # 创建基于用户ID和日期的随机数生成器以确保相同的描述文本
        seed_value = int(hashlib.md5(f"{user_id}_{datetime.now().strftime('%Y-%m-%d')}".encode()).hexdigest(), 16)
        random_gen = random.Random(seed_value)
        
        # 修改新抽签消息格式，避免特殊字符问题
        message = (
            f"🔮 {user_name}的今日运势 🔮\n\n"
            f"结果: {fortune}\n\n"
            f"{random_gen.choice(fortune_info['description'])}\n\n"
            f"健康: {random_gen.choice(fortune_info['health'])}\n"
            f"爱情: {random_gen.choice(fortune_info['love'])}\n"
            f"事业/学业: {random_gen.choice(fortune_info['career'])}\n\n"
            f"建议: {random_gen.choice(fortune_info['advice'])}"
        )
        
        # 保存用户抽签记录
        save_result = await save_user_fortune(user_id, fortune)
        
        if not save_result:
            logger.warning(f"用户 {user_id} 的抽签结果保存失败，但会继续显示结果")
        
        # 准备按钮
        # 好运势和坏运势的按钮文字不同
        if fortune in ["大吉", "中吉", "小吉"]:
            button_text = "✨ 接受好运 ✨"
        else:
            button_text = "🙏 祈求平安 🙏"
        
        keyboard = [
            [InlineKeyboardButton(button_text, callback_data=f"omikuji_{fortune}_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 尝试使用Markdown，如果失败则回退到纯文本
        try:
            await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode="MARKDOWN"
            )
        except telegram.error.BadRequest as e:
            logger.warning(f"Markdown格式发送失败，切换到纯文本: {e}")
            await update.message.reply_text(
                message,
                reply_markup=reply_markup
            )
        
        logger.info(f"用户 {user_id} 抽签成功，结果: {fortune}")
    except Exception as e:
        logger.error(f"抽签过程中出错: {str(e)}")
        await update.message.reply_text(
            "抱歉，抽签过程中出现错误。请稍后再试。\n"
            "Sorry, there was an error during the omikuji drawing. Please try again later."
        )


async def omikuji_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理抽签按钮回调
    """
    try:
        query = update.callback_query
        
        # 解析回调数据
        try:
            parts = query.data.split("_")
            if len(parts) != 3:
                raise ValueError("Invalid callback data format")
                
            _, fortune, user_id = parts
            user_id = int(user_id)
        except (ValueError, IndexError) as e:
            logger.error(f"解析回调数据时出错: {str(e)}")
            await query.answer("按钮数据无效，请尝试重新抽签", show_alert=True)
            return
        
        # 检查是否是抽签的用户在点击按钮
        if query.from_user.id != user_id:
            await query.answer("这不是您的御神签，无法进行互动。", show_alert=True)
            return
        
        # 根据运势类型提供不同的回应，修改消息格式
        if fortune in ["大吉", "中吉", "小吉"]:
            await query.answer("好运已经接受，愿它伴随着您！", show_alert=True)
            try:
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n✨ {query.from_user.first_name} 已接受好运 ✨",
                    parse_mode="MARKDOWN"
                )
            except telegram.error.BadRequest as e:
                logger.warning(f"Markdown格式编辑失败，切换到纯文本: {e}")
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n✨ {query.from_user.first_name} 已接受好运 ✨"
                )
            logger.info(f"用户 {user_id} 接受了好运")
        else:
            await query.answer("您已将不好的运势留在了神社，祈求平安！", show_alert=True)
            try:
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n🙏 {query.from_user.first_name} 已祈求平安 🙏",
                    parse_mode="MARKDOWN"
                )
            except telegram.error.BadRequest as e:
                logger.warning(f"Markdown格式编辑失败，切换到纯文本: {e}")
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n🙏 {query.from_user.first_name} 已祈求平安 🙏"
                )
            logger.info(f"用户 {user_id} 祈求了平安")
    except Exception as e:
        logger.error(f"处理回调时出错: {str(e)}")
        try:
            await query.answer("处理您的请求时出错，请稍后再试。", show_alert=True)
        except Exception as exc:
            logger.debug("回调错误提示发送失败: %s", exc)


def setup_omikuji_handlers(application):
    """
    设置御神签相关的命令处理器
    """
    logger.info("注册御神签命令处理器")
    application.add_handler(CommandHandler("omikuji", omikuji_command))
    application.add_handler(CallbackQueryHandler(omikuji_callback, pattern=r"^omikuji_"))
