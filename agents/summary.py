import logging
import json
import openai
import re
from pathlib import Path  
import os
from sentence_transformers import SentenceTransformer, util

class summary():
    def __init__(self , logger , prompt_dir="./doc", api_json = None):
        self.max_fail_cnt = 3
        self.token_used = 0
        self.prompt_template : dict[str , str] = None
        self.example : dict[str , str] = None
        self.player_name = None
        self.all_game_info = {
            "self_role" : "",
            "all_role_info" : "",
            "result" : "",
        }
        self.memory_stream = {} 
        self.operation_info = {}
        self.guess_role = {}
        self.chat_func = None
        self.chinese_to_english = {
            # summary
            "投票總結" : "vote",
            "發言總結" : "dialogue",
            "技能總結" : "operation",
        }
        self.operation_to_chinese = {
            "seer" : "預言家查驗，目標是",
            "witch" : "女巫的技能，目標是",
            "village" : "村民",
            "werewolf" : "狼人殺人，目標是",
            "werewolf_dialogue" : "狼人發言，想要殺掉",
            "hunter" : "獵人獵殺，目標是"
        }

        self.role_to_chinese = {
            "seer" : "預言家",
            "witch" : "女巫",
            "village" : "村民",
            "werewolf" : "狼人",
            "hunter" : "獵人"
        }

        self.player2identity =  [f"玩家{num}號" for num in range(10)] + [f"玩家{num}" for num in range(10)] 
        self.logger : logging.Logger = logger
        self.prompt_dir = Path(prompt_dir)
        self.__load_prompt_and_example__(self.prompt_dir)
        
        # openai api setting
        self.api_kwargs = {}
        try:
            self.__openai_init__(api_json)
        except:
            raise Exception("API Init failed")
        
        self.summary_limit = 20
        self.similarly_sentence_num = 5
        self.get_score_fail_times = 3
        self.embedding_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')

        # self.__load_game_info(file_path = "./game_info/11_09_20_44_iAgent932.jsonl")

    def __load_prompt_and_example__(self , prompt_dir):
        """load prompt json to dict"""
        self.logger.debug("load common json")
        with open(prompt_dir / "./prompt/summary/common_prompt.json" , encoding="utf-8") as json_file: self.prompt_template = json.load(json_file)
        with open(prompt_dir / "./prompt/summary/common_example.json" , encoding="utf-8") as json_file: self.example = json.load(json_file)

        for key , prompt_li in self.prompt_template.items():
            self.prompt_template[key] = '\n'.join(prompt_li)
        for key , prompt_li in self.example.items():
            self.example[key] = '\n'.join(prompt_li)

    def __openai_init__(self , api_json):
        """azure openai api setting , can override this"""
        with open(api_json,'r') as f : api_info = json.load(f)
        openai.api_key = api_info["key"]

        if api_info["api_type"] == "azure":
            openai.api_type = api_info["api_type"]
            openai.api_base = api_info["api_base"]
            openai.api_version = api_info["api_version"] 
            self.api_kwargs["engine"] = api_info['engine']
        else:
            self.api_kwargs["model"] = api_info["model"]
    

    def __openai_send__(self , prompt):
        """openai api send prompt , can override this."""
        response = openai.ChatCompletion.create(
            **self.api_kwargs,
            messages = [
                # You are a helpful assistant.
                {"role":"system","content":"You are an AI assistant that helps people find information."},
                {"role":"user","content":prompt}
            ],
            temperature=0.7,
            max_tokens=800,
            top_p=0.95,
            frequency_penalty=0,
            presence_penalty=0,
            stop=None)
        
        return response['choices'][0]['message']['content']
    
    def __process_LLM_output__(self , prompt , keyword_list , sample_output):
        """
        communication with LLM , repeat {self.max_fail_cnt} util find the {keyword_list} in LLM response .
        return the {keyword_list} dict , if fail get {keyword_list} in LLM response , return {sample_output}.
        """
        success_get_keyword = False
        fail_idx = 0

        self.logger.debug(f"LLM keyword : {keyword_list}")
        info = {}

        while not success_get_keyword and fail_idx < self.max_fail_cnt:

            self.logger.debug(f"start {fail_idx} prompt")
            info = {}
            result = self.__openai_send__(prompt)

            # result block by openai
            if result == None:
                fail_idx+=1
                continue
            

            splited_result = result.split('\n')
            keyword_name = ""
            for line in splited_result:
                # get keyword like [XXX]
                keyword = re.search('\[(.*)\]', line)
                if keyword != None and keyword.group(1) in self.chinese_to_english.keys():
                    keyword_name = self.chinese_to_english[keyword.group(1)]
                    info[keyword_name] = ""
                elif keyword_name != "":
                    info[keyword_name] += line + "\n"

            if all(_ in info.keys() for _ in keyword_list): success_get_keyword = True
            else : fail_idx+=1
        
        self.logger.debug(f"LLM output : {info}")
        # print(f"LLM output : {info}")

        if fail_idx >= self.max_fail_cnt: info = sample_output

        return info
    
    def __process_user_role(self, data):
        
        role_info = ""
        for idx, key in enumerate(data):
            role_info += f"{idx}. {data[key]['user_name']}({idx})是{self.role_to_chinese[data[key]['user_role']]}\n"
        
        return role_info
    
    def __process_guess_role(self, stage, data):
        
        guess_info = ""
        for idx, role in enumerate(data['guess_role']):
            num = str(idx)
            guess_info += f"{idx}. {self.player_name[num]['user_name']}({idx})可能是{role}\n"

        day = stage.split('-')[0]
        if day != "check_role":
            self.guess_role[day] = guess_info


    def __memory_stream_push(self, stage, ob):
        day = stage.split('-')[0]
        if day in self.memory_stream.keys():
            self.memory_stream[day] += ob
        else:
            self.memory_stream[day] = ob

    def __operation_info_push(self, stage, ob):
        day = stage.split('-')[0]
        if day in self.operation_info.keys():
            self.operation_info[day] += ob
        else:
            self.operation_info[day] = ob

    
    def __process_announcement__(self , data):
        """add announcement to memory stream"""
        announcement = data['announcement']

        if any(data["vote_info"].values()) :
            self.__push_vote_info__(data["vote_info"] , data["stage"])

        for anno in announcement:
            ob = ""
            if len(anno['user']) > 0:
                player = str(anno['user'][0])
            if anno["operation"] == "chat":
                ob = f"{self.player_name[player]['user_name']}({player})說「{anno['description']}」\n"    
            elif anno["operation"] == "died":
                ob = f"{self.player_name[player]['user_name']}({player})死了\n"    
            
            self.__memory_stream_push(data["stage"], ob)

    def __info_init(self, stage):

        if stage.split('-')[0] != "check_role":

            day = str(int(stage.split('-')[0]))

            if day not in self.memory_stream.keys():
                self.memory_stream[day] = ""
            if day not in self.operation_info.keys():
                self.operation_info[day] = ""
            if day not in self.guess_role.keys():
                self.guess_role[day] = ""

    def __push_vote_info__(self , vote_info : dict , stage):
        """add vote info to memory stream"""
        prefix = "狼人投票殺人階段:" if stage.split('-')[-1] == "seer" else "玩家票人出去階段:"

        ob = ""
        for player , voted in vote_info.items():
            if voted != -1:
                ob += f"{prefix} {self.player_name[player]['user_name']}({player})投給{self.player_name[str(voted)]['user_name']}({voted})\n"
            else:
                ob += f"{prefix} {self.player_name[player]['user_name']}({player})棄票\n"

        self.__memory_stream_push(stage, ob)

    def __load_game_info(self, file_path = None, game_info = None):       

        if file_path != None:
            with open(self.prompt_dir / file_path, encoding="utf-8") as json_file: game_info = [json.loads(line) for line in json_file.readlines()]
        for val in game_info[0].values():
            self.my_player_role = val
        
        self.player_name = game_info[1]
        self.all_game_info["self_role"] = self.role_to_chinese[list(game_info[0].values())[0]]
        self.all_game_info["all_role_info"] = self.__process_user_role(game_info[1])
        for number, info in self.player_name.items():
            user_name = info["user_name"]
            self.player2identity.extend([user_name, f"{user_name}({number})"])

        no_save_op = ["dialogue", "vote1", "vote2"]
        for idx, info in enumerate(game_info):

            # stage info
            if "stage" in info.keys():
                self. __info_init(info["stage"])
                self.__process_announcement__(info)

                if "guess_role" in game_info[idx+1].keys():
                    self.__process_guess_role(info["stage"] , game_info[idx+1])

            # operation
            elif "stage_name" in info.keys() and (not info['stage_name'].split('-')[-1] in no_save_op):
                self. __info_init(info["stage_name"])
                ob = f"你使用了{self.operation_to_chinese[info['stage_name'].split('-')[-1]]}{info['target']}號玩家\n"
                self.__operation_info_push(info["stage_name"], ob)
                
                if "guess_role" in game_info[idx+1].keys():
                    self.__process_guess_role(info["stage_name"], game_info[idx+1])

        for anno in game_info[-2]["announcement"]:
            if anno["operation"] == "game_over":
                self.all_game_info["result"] = anno["description"]

        # print(self.all_game_info)
        # for i in range(1, len(self.memory_stream)+1):
        #     day = str(i)
        #     print(f"第{day}天")
        #     print(f"memory_stream: {self.memory_stream[day]}")
        #     print(f"operation_info: {self.operation_info[day]}")
        #     print(f"guess_role: {self.guess_role[day]}")


    def get_summary(self, file_name = "10_31_14_21.jsonl"):

        self.logger.debug("load game info")
        with open(f"generative_agent_with_werewolf_kill/doc/game_info/{file_name}" , encoding="utf-8") as json_file: game_info = [json.loads(line) for line in json_file.readlines()]
        for anno in game_info[-1]["announcement"]:
            if anno["operation"] == "game_over":
                result = anno["description"]

        # 分天summary
        day = 1
        for info in game_info:
            if "stage" in info:
                if  "-" in info["stage"] and day!=int(info["stage"].split("-")[0]):
                    day_str = f"第{day}天"
                    # vote、dialogue、operation summary
                    all_summary = self.__get_day_summary__(day_str, self.memory_stream, self.operation_info, result)
                    # self.__write_summary_score(all_summary, role="女巫")
                    
                    day = int(info["stage"].split("-")[0])
                    self.memory_stream = ""
                    self.operation_info = ""

                self.__process_announcement__(info)
            elif info['stage_name'].split('-')[-1] != "check":
                self.operation_info += f"你使用了{self.operation_to_chinese[info['stage_name'].split('-')[-1]]}是{info['target']}號玩家\n"
        
        day_str = f"第{day}天"
        all_summary = self.__get_day_summary__(day_str, self.memory_stream, self.operation_info, result)
        # self.__write_summary_score(all_summary, role="女巫")

    def __get_day_summary__(self, day, day_memory, day_operation, result):
        """day summary to openai"""
        print("day summary")
        self.logger.debug(f"day summary")        
        self.max_fail_cnt = 3
        # memory_str = self.__memory_to_str__(day_memory)
        # player_list = self.__get_player_list__()
        player_list = ""
        final_prompt = self.prompt_template['day_summary'].replace("%l" , self.example['day_summary']).replace("%z", day).replace("%m" , day_memory).replace("%o" , day_operation).replace("%y" , player_list).replace("%p" , result)
        print(f"final_prompt = {final_prompt}")
        info = {
            "vote" : "vote_summary",
            "dialogue" : "dialogue_summary",
            "operation" : "operation_summary",
        }        
        # info = self.__process_LLM_output__(final_prompt , ["vote", "dialogue", "operation"] , info)

        return info['vote'], info['dialogue'], info['operation']

    def __write_summary_score(self, summary , role):
        """summary + score"""
        self.set_score(role, "vote", summary[0])
        self.set_score(role, "dialogue", summary[1])
        self.set_score(role, "operation", summary[2])

    def set_score(self, role, stage, summary):

        final_prompt = self.prompt_template["score"].replace("%s", summary)
        self.logger.debug("Prompt: "+str(final_prompt))
        response = self.__openai_send__(final_prompt)
        self.logger.debug("Response: "+str(response))
        try:
            score = response.split(":")[1]
        except:
            self.logger.debug("Error: Don't match key")
            self.get_score_fail_times -= 1
            if self.get_score_fail_times >= 0:
                self.set_score(role= role, stage= stage, summary= summary)

        file_path = os.path.join(role, f"{stage}.json")
        try:
            summary_set = self.__load_summary(file_path= file_path)
        except:
            summary_set = []
        updated_summary_set = self.__update_summary(summary_set= summary_set, summary= summary, score= score)
    
        self.__write_summary(file_path= file_path, data= updated_summary_set)

    def __load_summary(self, file_path):
        
        with open(self.prompt_dir / file_path, encoding="utf-8") as json_file: summary_set = json.load(json_file)
        return summary_set
    
    def __write_summary(self, file_path, data):

        try:
            with open(self.prompt_dir / file_path, "w") as json_file: 
                new_data = json.dumps(data, indent= 1)
                json_file.write(new_data)
        except:
            os.mkdir(self.prompt_dir / file_path.split("\\")[0])
            self.__write_summary(file_path, data)
        self.get_score_fail_times = 3

    def __update_summary(self, summary_set, summary, score):
        
        summary_set.append({"summary": summary, "score": score})
        summary_set = sorted(summary_set, key= lambda x : x["score"], reverse= True)
        
        if len(summary_set) > self.summary_limit:            
            summary_set.pop()
        return summary_set
    
    def __get_current_summary(self, game_info):

        self.__load_game_info(game_info= game_info)
    
        for i in range(1, len(self.memory_stream)+1):
            day = str(i)
            self.prompt_template['current_summary'] += f"[第{day}天遊戲資訊]\n"
            self.prompt_template['current_summary'] += f"{self.memory_stream[day]}\n"

            self.prompt_template['current_summary'] += f"[第{day}天猜測其他玩家的身分]\n"
            self.prompt_template['current_summary'] += f"{self.guess_role[day]}\n"
            
            if len(self.operation_info[day]) != 0:
                self.prompt_template['current_summary'] +=f"[你所進行的操作]\n"
                self.prompt_template['current_summary'] += f"{self.operation_info[day]}\n"

        self.prompt_template['current_summary'] = self.prompt_template['current_summary'].replace("%l", self.example['current_summary'])
        self.prompt_template['current_summary'] += f"* 回應\n"
        self.prompt_template['current_summary'] += f"[目前總結]\n"
        print(self.prompt_template['current_summary'])
        
        # print(self.prompt_template['current_summary'])
    def transform_player2identity(self, summary):
        
        for player_number in self.player2identity:
            if player_number in summary:
                identity = self.role_to_chinese[self.all_player_role[player_number[2]]["user_role"]]
                summary = summary.replace(player_number, f"{identity}")

        return summary


    def find_similarly_summary(self, stage, game_info):
        
        self.__get_current_summary(game_info= game_info)

        file_path = os.path.join(self.my_player_role, f"{stage}.json")
        summary_set = self.__load_summary(file_path= file_path)
        similarly_scores = []
        for idx, summary_each in enumerate(summary_set):
            embeddings = self.embedding_model.encode([summary_each, game_info])
            cos_sim = util.cos_sim(embeddings, embeddings)
            similarly_scores.append([cos_sim[0][1], idx])

        similarly_scores = sorted(similarly_scores,key= lambda x: x[1], reverse= True)

        return similarly_scores[0: self.similarly_sentence_num]


    
if __name__ == '__main__':

    s = summary(logger = logging.getLogger(__name__), api_json="./doc/secret/api.json")
    # s = summary(logger = logging.getLogger(__name__), prompt_dir="./generative_agent_with_werewolf_kill/doc", api_json = "./generative_agent_with_werewolf_kill/doc/secret/openai.key")