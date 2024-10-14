from django.shortcuts import render
from django.db import transaction
from django.http import FileResponse
from django.conf import settings

from rest_framework.decorators import api_view
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView
from rest_framework.exceptions import NotFound

from langchain_openai import ChatOpenAI
from langchain.prompts import PromptTemplate

from console.serializers import GetOrdersSerializer, OrderSerializer, \
                                GetOrderSerializer
from console.models import Customer, Order, KnowledgeFileItem, Session, Applicant
from console.ai.generate_text import ai_interviewer

from dotenv import load_dotenv

import os
load_dotenv()

class AgentViewSet(ModelViewSet):
    http_method_names = ['post']
    queryset = Order.objects.all()
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    serializer_class = OrderSerializer

    def get_serializer_context(self):
        return {'request': self.request}

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            order = serializer.save()

            return Response({"agent_id": f"{serializer.instance.id}"}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class GetAgentsViewSet(ModelViewSet):
    http_method_names = ['get']
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        return GetOrdersSerializer

    def get_queryset(self):
        customer_id = Customer.objects.only('id').get(user_id=self.request.user.id)
        return Order.objects.filter(customer_id = customer_id)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

class GetAgentViewSet(ModelViewSet):
    http_method_names = ['get']
    permission_classes = [IsAuthenticated]
    lookup_field = 'order_id'

    def get_serializer_class(self):
        return GetOrderSerializer

    def get_queryset(self):
        customer_id = Customer.objects.only('id').get(user_id=self.request.user.id)
        return Order.objects.filter(customer_id = customer_id)

    def retrieve(self, request, *args, **kwargs):
        order_id = self.kwargs.get(self.lookup_field)
        order = self.get_queryset().filter(id=order_id).first()

        if order is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(order)
        return Response(serializer.data, status=status.HTTP_200_OK)

class ManageAgentViewSet(ModelViewSet):
    http_method_names = ['patch', 'delete']
    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer
    lookup_field = 'id'

    def get_queryset(self):
        order_id = self.kwargs.get(self.lookup_field)
        customer = Customer.objects.only('id').get(user_id=self.request.user.id)
        return Order.objects.filter(id=order_id, customer=customer)

    @transaction.atomic
    def perform_destroy(self, instance):
        if instance.agent:
            if hasattr(instance.agent, 'identity'):
                identity = instance.agent.identity
                if identity.avatar:
                    identity.avatar.delete(save=False)
                identity.delete()

            if hasattr(instance.agent, 'behaviour'):
                instance.agent.behaviour.delete()

            if hasattr(instance.agent, 'knowledge'):
                knowledge = instance.agent.knowledge
                if hasattr(knowledge, 'knowledgefileitem'):
                    file_items = KnowledgeFileItem.objects.filter(knowledge=knowledge)
                    for file_item in file_items:
                        if file_item.file_item:
                            file_item.file_item.delete(save=False)
                        file_item.delete()
                knowledge.delete()

            # Delete the agent
            instance.agent.delete()

        # Delete the order
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @transaction.atomic
    def update(self, request, *args, **kwargs):

        partial = kwargs.pop('partial', True)
        instance = self.get_object()

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        if serializer.is_valid(raise_exception=True):
            updated_order_data = serializer.save()

            return Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class SecureFileAccessView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id, filename):
        try:
            order = Order.objects.get(id=order_id, customer__user_id=request.user.id)
        except Order.DoesNotExist:
            return Response({"detail": "You don't have permission to access this file"}, status=status.HTTP_403_FORBIDDEN)

        file_path = os.path.join(settings.MEDIA_ROOT, 'files', f"{order_id}__{filename}")
        if not os.path.exists(file_path):
            return Response({"detail": "File Not found"}, status=status.HTTP_404_NOT_FOUND)

        return FileResponse(open(file_path, 'rb'))

class SecureAvatarAccessView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id, filename):
        try:
            order = Order.objects.get(id=order_id, customer__user_id=request.user.id)
        except Order.DoesNotExist:
            return Response({"detail": "You don't have permission to access this file"}, status=status.HTTP_403_FORBIDDEN)

        file_path = os.path.join(settings.MEDIA_ROOT, 'avatars', f"{order_id}__{filename}")
        if not os.path.exists(file_path):
            return Response({"detail": "File Not found"}, status=status.HTTP_404_NOT_FOUND)

        return FileResponse(open(file_path, 'rb'))

@api_view(['GET'])
def interview_session_create(request, agent_id):
    try:
        order = Order.objects.get(id=agent_id)
        data = request.data
        if request.user.is_authenticated:
            user_email = request.user.email
        else:
            user_email = request.data.get('email')
            if not user_email:
                return Response({"detail": "Email is missing"}, status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            applicant, _ = Applicant.objects.get_or_create(email = user_email)
            session, session_created = Session.objects.get_or_create(order=order, applicant=applicant)
            session.ready = False
            session.save()

            if session_created:
                greeting = order.agent.behaviour.agent_greeting
                session.last_question = greeting
                session.save()
            else:
                if session.n_questions == 0 and not session.last_question:
                    greeting = f"Hеllo again, I am {order.agent.identity.agent_name}, your ai interviewer and we started a interview previously. Please describe your experience and skills related to the position you're applying for"
                elif session.n_questions == 0 and not session.last_answer:
                    greeting = f"Hi, I am {order.agent.identity.agent_name} and we started a session. Tell me when you're ready to start with the questions?"
                elif session.n_questions != 0:
                    greeting = f"Hi, I am {order.agent.identity.agent_name} and we started a interview process. Tell me when you're ready to to continue with the questions?"
            session.last_question = greeting
            session.save()

            return Response({
                "ai_text": greeting,
                "final": session.final,
                "score": session.score,
                "confidence" : session.confidence
            }, status=status.HTTP_200_OK)

    except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'{e}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def interview_session_flow(request, agent_id):
    try:
        order = Order.objects.get(id=agent_id)
        data = request.data
        if request.user.is_authenticated:
            user_email = request.user.email
        else:
            user_email = request.data.get('email')
            if not user_email:
                return Response({"detail": "Email is missing"}, status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            applicant = Applicant.objects.get(email = user_email)
            session = Session.objects.get(order=order, applicant=applicant)

            human_text = request.data.get('text')

            if session.n_questions == 0 and not session.last_question:
                session.ready = False
                session.save()

                ai_text = f"We started a interview process with you, can you tell me your skills, please?"

            elif session.n_questions == 0 and not session.last_answer:
                session.ready = False
                session.save()

                llm = ChatOpenAI(openai_api_key=os.getenv('OPENAI_API_KEY'), model="gpt-4o-mini", temperature=0)

                prompt = PromptTemplate(
                    input_variables=["text"],
                    template="Analyze the following text and determine if it mentions any skills:\n\n{text}\n\nOutput '0' if no skills are mentioned and '1' if skills are mentioned."
                )
                format_prompt = prompt.format(text=human_text)
                response = llm.invoke(format_prompt)

                if hasattr(response, 'content'):
                    skills_provided = response.content
                elif hasattr(response, 'message'):
                    skills_provided = response.message
                else:
                    skills_provided = response

                print("------------------------------")
                print(skills_provided)

                if skills_provided == "0":
                    missing_skills_prompt = PromptTemplate(
                        input_variables=["text"],
                        template="You are voice interviewer bot and the job applicant has not provided any skills. Analyze the following text for tone:\n\n{text}\n\nGenerate a polite message asking them to provide their skills again, or indicate if the text is offensive."
                    )
                    formated_missing_skills_prompt = missing_skills_prompt.format(text=human_text)
                    response = llm.invoke(formated_missing_skills_prompt)

                    if hasattr(response, 'content'):
                        missing_skills_message = response.content
                    elif hasattr(response, 'message'):
                        missing_skills_message = response.message
                    else:
                        missing_skills_message = response

                    ai_text = missing_skills_message
                else:
                    skills_description_prompt = PromptTemplate(
                        input_variables=["text"],
                        template="Based on the following text: \n\n{text}\n\n, generate a short description that highlights the skills mentioned. \nProvide a concise summary for what the user know and what does not know!"
                    )
                    formated_skills_description_prompt = skills_description_prompt.format(text=human_text)
                    response = llm.invoke(formated_skills_description_prompt)

                    if hasattr(response, 'content'):
                        short_description = response.content
                    elif hasattr(response, 'message'):
                        short_description = response.message
                    else:
                        short_description = response

                    applicant.skills = short_description
                    applicant.save()

                    session.last_answer = short_description
                    session.save()

                    readiness_message_prompt = PromptTemplate(
                        template="You are voice interviewer bot and the user just Generate a message to inform the user that their skills have been noted, and they will be asked questions based on their job description and skills. Ask them to give you information when they are ready."
                    )
                    formated_readiness_message_prompt = readiness_message_prompt.format()

                    response = llm.invoke(formated_readiness_message_prompt)

                    if hasattr(response, 'content'):
                        ai_text = response.content
                    elif hasattr(response, 'message'):
                        ai_text = response.message
                    else:
                        ai_text = response

            elif session.n_questions == 0 and session.last_answer and not session.ready:
                llm = ChatOpenAI(openai_api_key=os.getenv('OPENAI_API_KEY'), model="gpt-4o-mini", temperature=0)

                prompt = PromptTemplate(
                    input_variables=["text"],
                    template="Analyze the following text and determine if the user mentions that is ready for starting the interview process:\n\n{text}\n\nOutput '0' if the user is not ready and '1' if the user is ready."
                )
                formated_prompt = prompt.format(text=human_text)
                response = llm.invoke(formated_prompt)

                if hasattr(response, 'content'):
                    is_ready = response.content
                elif hasattr(response, 'message'):
                    is_ready = response.message
                else:
                    is_ready = response

                if is_ready == "0":
                    user_said_no = PromptTemplate(
                        input_variables=["text"],
                        template="You are voice interviewer, and the user is not ready yet to be interviewed. Analyze the following text for tone:\n\n{text}\n\nGenerate a polite message that you are waiting on user to be ready to be interviewed. If the text is offensive indicate to the user to use more natural human language."
                    )
                    formated_user_said_no = user_said_no.format(text=human_text)
                    response = llm.invoke(formated_user_said_no)

                    if hasattr(response, 'content'):
                        ai_text = response.content
                    elif hasattr(response, 'message'):
                        ai_text = response.message
                    else:
                        ai_text = response
                else:
                    session.ready = True
                    session.save()
                    score, ai_text = ai_interviewer(text = human_text, session = session)
            elif session.n_questions != 0 and session.ready == False:
                llm = ChatOpenAI(openai_api_key=os.getenv('OPENAI_API_KEY'), model="gpt-4o-mini", temperature=0)
                previous_context_prompt = PromptTemplate(
                    input_variables=["last_question"],
                    template="You are voice interviewer. Ask the user if he is ready to continue with the interview:\n\nLast question: {last_question}\n\nAre you ready to continue?"
                )
                formated_previous_context_prompt = previous_context_prompt.format(last_question=session.last_question)
                response = llm.invoke(formated_previous_context_prompt)

                if hasattr(response, 'content'):
                    ai_text = response.content
                elif hasattr(response, 'message'):
                    ai_text = response.message
                else:
                    ai_text = response
            else:
                score, ai_text = ai_interviewer(text = human_text, session = session)

                if session.final:
                    ai_text = f"Thanks for your time, we finished with the interview! Your score is {int(session.score)} percents. Have a good day."

        return Response({
            "ai_text": ai_text,
            "final": session.final,
            "score": session.score,
            "confidence" : session.confidence
        }, status=status.HTTP_200_OK)

    except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'error': f'{e}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
