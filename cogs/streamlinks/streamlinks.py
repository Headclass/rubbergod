import discord
from discord.ext import commands
from repository.stream_links_repo import StreamLinksRepo
from ..room_check import RoomCheck
from config import app_config
from config.messages import Messages
from typing import List, Union
import shlex
import utils
from repository.database.stream_link import StreamLink
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

# Pattern: "AnyText | [Subject] Page: CurrentPage / {TotalPages}"
pagination_regex = re.compile(r'^\[([^\]]*)\]\s*Page:\s*(\d*)\s*\/\s*(\d*)')
# Pattern (ISO Date): "YYYY-MM-DD"
iso_date_regex = re.compile(r'(\d*)-(\d*)-(\d*)')


class StreamLinks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.repo = StreamLinksRepo()
        self.check = RoomCheck(bot)
        self.config = app_config.Config

    @commands.cooldown(rate=2, per=30.0, type=commands.BucketType.user)
    @commands.group(brief=Messages.streamlinks_brief, usage="<subject>")
    async def streamlinks(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            parameters = ctx.message.content[len('streamlinks') + 1:].strip()
            params_arr = shlex.split(parameters)
            if len(params_arr) == 1:
                await self.get_streamlinks(ctx, params_arr[0])
            else:
                await ctx.reply(content=Messages.streamlinks_format)

    async def get_streamlinks(self, ctx: commands.Context, subject: str):
        streamlinks = self.repo.get_streamlinks_of_subject(subject)

        embed = self.create_embed_of_link(streamlinks[0], ctx.author, len(streamlinks), 1)
        message: discord.Message = await ctx.reply(embed=embed)
        await utils.add_pagination_reactions(message, len(streamlinks))

    @commands.check(utils.helper_plus)
    @streamlinks.command(brief=Messages.streamlinks_add_brief)
    async def add(self, ctx: commands.Context, subject: str, link: str, user: Union[discord.User, discord.Member, str], *args):
        try:
            await ctx.message.add_reaction(self.config.emote_loading)

            username = user if type(user) == str else user.display_name
            link = utils.clear_link_escape(link)
            args = list(args)

            if self.repo.exists_link(link):
                await self.replace_reaction(ctx, "❌")
                await ctx.reply(utils.fill_message('streamlinks_add_link_exists', user=ctx.author.id))
                return

            link_data = self.get_link_data(link)
            is_iso_date = iso_date_regex.match(args[0]) is not None
            if link_data['upload_date'] is None:
                if is_iso_date:
                    link_data['upload_date'] = datetime.strptime(args[0], '%Y-%m-%d')
                    del args[0]
                else:
                    link_data['upload_date'] = datetime.utcnow()
            else:
                if is_iso_date:
                    del args[0]

            self.repo.create(subject.lower(), link, username,
                             " ".join(args), link_data['image'], link_data['upload_date'])
            await ctx.reply(content=Messages.streamlinks_add_success)
            await self.replace_reaction(ctx, "✅")
        except:
            await self.replace_reaction(ctx, "❌")
            raise

    def get_link_data(self, link: str):
        """
        Gets thumbnail from youtube or maybe from another service.
        It downloads HTML from link and tries get thumbnail url from SEO meta tags.
        """
        data = {
            'image': None,
            'upload_date': None
        }

        response = requests.get(link)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.content, 'html.parser')
        meta_tags = soup.select('meta')

        for meta in meta_tags:
            if 'property' in meta.attrs and meta.attrs['property'] == 'og:image':
                data['image'] = meta.attrs['content']
            if 'itemprop' in meta.attrs and meta.attrs['itemprop'] in ['datePublished', 'uploadDate']:
                data['upload_date'] = datetime.strptime(meta.attrs['content'], '%Y-%m-%d')

        return data

    async def replace_reaction(self, ctx: commands.Context, emoji: str):
        try:
            await ctx.message.clear_reactions()
            await ctx.message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    def create_embed_of_link(self, streamlink: StreamLink, author: Union[discord.User, discord.Member],
                             links_count: int, current_pos: int) -> discord.Embed:
        embed = discord.Embed(color=0xEEE657)
        embed.set_author(name="Streamlinks")

        if streamlink.thumbnail_url is not None:
            embed.set_image(url=streamlink.thumbnail_url)

        embed.add_field(name="Předmět", value=streamlink.subject.upper(), inline=True)
        embed.add_field(name="Od", value=streamlink.member_name, inline=True)
        embed.add_field(name="Datum vydání", value=streamlink.created_at.strftime("%d. %m. %Y"), inline=True)
        embed.add_field(name="Odkaz", value=f"[{streamlink.link}]({streamlink.link})", inline=False)
        embed.add_field(name="Popis", value=streamlink.description, inline=False)
        embed.timestamp = datetime.utcnow()
        utils.add_author_footer(embed, author, additional_text=[
                                f"[{streamlink.subject.upper()}] Page: {current_pos} / {links_count}"])

        return embed

    async def handle_reaction(self, ctx):
        try:
            if ctx['reply_to'] is None:  # Reply is required to render embed.
                await ctx['message'].edit(content=Messages.streamlinks_missing_original, embed=None)
                return

            embed: discord.Embed = ctx['message'].embeds[0]
            footer_text = embed.footer.text.split('|')[1].strip()
            match = pagination_regex.match(footer_text)

            if match is None:
                await ctx['message'].edit(content=Messages.streamlinks_unsupported_embed, embed=None)
                return  # Invalid or unsupported embed.

            groups = match.groups()
            subject = str(groups[0]).lower()
            current_page = int(groups[1])
            pages_count = int(groups[2])

            streamlinks = self.repo.get_streamlinks_of_subject(subject)

            if len(streamlinks) != pages_count:  # New streamlink was added, but removed.
                await ctx['message'].edit(content=Messages.streamlinks_not_actual, embed=None)
                return

            new_page = utils.pagination_next(ctx['emoji'], current_page, pages_count)
            if new_page <= 0 or new_page == current_page:
                return  # Pagination to same page. We can ignore.

            # Index - 1, because index position.
            streamlink: StreamLink = streamlinks[new_page - 1]
            embed = self.create_embed_of_link(streamlink, ctx['reply_to'].author, len(streamlinks), new_page)
            await ctx['message'].edit(embed=embed)
        finally:
            if ctx["message"].guild:  # cannot remove reaction in DM
                await ctx["message"].remove_reaction(ctx["emoji"], ctx["member"])

    @add.error
    async def streamlinks_add_error(self, ctx: commands.Context, error):
        if isinstance(error, discord.ext.commands.MissingRequiredArgument):
            await ctx.send(Messages.streamlinks_add_format)